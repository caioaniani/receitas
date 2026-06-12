"""Auto-sync Seru → EstoqueLoja (cron 15 minutos).

APScheduler roda dentro de cada worker gunicorn. Pra evitar execucao
duplicada quando ha multiplos workers, usamos pg_try_advisory_lock no
PostgreSQL — so 1 worker pega o lock por vez, os outros pulam.

Ativado por default. Pra desligar em runtime: setar env var
SERU_AUTO_SYNC=0 antes do startup.
"""
import logging
import os
from datetime import timedelta

from sqlalchemy import text

from app.utils import hoje as hoje_brt

logger = logging.getLogger(__name__)


def _catchup_dias():
    """Quantos dias pra tras o sync reprocessa (catch-up).

    Default 2 (hoje + ontem + anteontem). Cobre falhas de sync de ate ~2 dias
    sem perder vendas. Idempotencia (PK de *PedidoProcessado) garante que
    pedidos ja baixados sao pulados — sem dupla-baixa. Configuravel via env
    SYNC_CATCHUP_DIAS.
    """
    try:
        return max(0, int(os.environ.get('SYNC_CATCHUP_DIAS', '2')))
    except (TypeError, ValueError):
        return 2

_scheduler = None
_ult_run = None
_ult_run_vnda = None
_ult_run_backup = None
_ult_run_backup_chatwoot = None
LOCK_KEY = 7723  # advisory lock pro Seru
LOCK_KEY_VNDA = 7724  # advisory lock pro VNDA
LOCK_KEY_BACKUP = 7731  # advisory lock pro backup diario
LOCK_KEY_BACKUP_CHATWOOT = 7735  # advisory lock pro backup do banco do Chatwoot
LOCK_KEY_VNDA_CARD = 7736  # advisory lock pro cache de pedidos do site (card CRM)
LOCK_KEY_VIGIA_ABANDONO = 7737  # advisory lock pro detector de conversas abandonadas
LOCK_KEY_AUDITOR = 7738  # advisory lock pro auditor diario do bot
LOCK_KEY_VIGIA_CHATWOOT = 7739  # advisory lock pro vigia de infra do Chatwoot


def _com_lock(key, fn, label='job'):
    """Roda fn() protegida por advisory lock de sessao, com lock E unlock na
    MESMA conexao. Critico: advisory lock e por sessao — se o unlock roda
    noutra conexao do pool (bug antigo), o lock fica PRESO e os jobs seguintes
    pulam pra sempre. Em SQLite/nao-pg roda direto. Se outro worker ja tem o
    lock, pula silenciosamente."""
    from app.extensions import db
    if db.engine.dialect.name != 'postgresql':
        try:
            fn()
        except Exception:
            logger.exception('%s falhou', label)
        return
    conn = db.engine.connect()
    try:
        got = bool(conn.execute(text('SELECT pg_try_advisory_lock(:k)'),
                                {'k': key}).scalar())
        if not got:
            return
        try:
            fn()
        except Exception:
            logger.exception('%s falhou', label)
        finally:
            try:
                conn.execute(text('SELECT pg_advisory_unlock(:k)'), {'k': key})
            except Exception:
                pass
    finally:
        conn.close()


def status():
    """Retorna info do scheduler pra UI mostrar (Seru).
    `ultimo_run` e um datetime UTC (pra template aplicar filtro |brt)."""
    return {
        'ativo': _scheduler is not None and _scheduler.running,
        'ultimo_run': _ult_run,
    }


def status_vnda():
    """Status do job VNDA."""
    return {
        'ativo': _scheduler is not None and _scheduler.running,
        'ultimo_run': _ult_run_vnda,
    }


def _run_sync(app):
    """Job: roda 1 ciclo de processar_pedidos da Seru."""
    global _ult_run
    from app.extensions import db
    from app.services import seru_sync
    from app.utils import agora as _agora

    with app.app_context():
        uri = app.config.get('SQLALCHEMY_DATABASE_URI', '') or ''
        is_pg = 'postgresql' in uri
        conn = db.engine.connect()
        try:
            if is_pg:
                got = conn.execute(text('SELECT pg_try_advisory_lock(:k)'),
                                   {'k': LOCK_KEY}).scalar()
                if not got:
                    return  # outro worker esta executando
            try:
                # Catch-up: processa de D-N ate hoje (BRT). Se o sync ficou
                # fora do ar, retenta os dias perdidos. Idempotencia
                # (SeruPedidoProcessado PK) pula ja-processados — sem
                # dupla-baixa. So pega faltantes + estorna cancelados.
                hoje = hoje_brt()
                inicio = hoje - timedelta(days=_catchup_dias())
                stats = seru_sync.processar_pedidos(inicio, hoje, user=None)
                _ult_run = _agora()
                # Persiste timestamp pra sobreviver a deploy (memoria zera).
                from app.models import AppConfig
                AppConfig.set('seru_ultimo_sync', _ult_run.isoformat())
                db.session.commit()
                ativas = any(stats.get(k, 0) for k in (
                    'pedidos_novos', 'itens_baixados',
                    'pedidos_cancelados_estornados'))
                if ativas:
                    logger.info('seru auto-sync (com mudancas): %s', stats)
                else:
                    logger.debug('seru auto-sync (sem mudancas)')
            except Exception:
                logger.exception('seru auto-sync falhou')
            finally:
                if is_pg:
                    try:
                        conn.execute(text('SELECT pg_advisory_unlock(:k)'),
                                     {'k': LOCK_KEY})
                    except Exception:
                        pass
        finally:
            conn.close()


def _run_vnda_sync(app):
    """Job: roda 1 ciclo VNDA pra data de entrega = HOJE (BRT)."""
    global _ult_run_vnda
    from app.extensions import db
    from app.services import vnda_sync
    from app.utils import agora as _agora

    with app.app_context():
        uri = app.config.get('SQLALCHEMY_DATABASE_URI', '') or ''
        is_pg = 'postgresql' in uri
        conn = db.engine.connect()
        try:
            if is_pg:
                got = conn.execute(text('SELECT pg_try_advisory_lock(:k)'),
                                   {'k': LOCK_KEY_VNDA}).scalar()
                if not got:
                    return
            try:
                # Catch-up: VNDA processa por data de entrega (1 data por
                # chamada), entao itera de hoje ate D-N. Idempotencia
                # (VndaPedidoProcessado PK) evita dupla-baixa.
                hoje = hoje_brt()
                for d in range(_catchup_dias() + 1):
                    dia = hoje - timedelta(days=d)
                    stats = vnda_sync.processar_pedidos(dia, user=None)
                    if stats.get('erro'):
                        logger.warning('vnda auto-sync erro (%s): %s',
                                       dia, stats['erro'])
                    elif any(stats.get(k, 0) for k in (
                            'pedidos_novos', 'itens_baixados',
                            'pedidos_cancelados_estornados')):
                        logger.info('vnda auto-sync %s (com mudancas): %s',
                                    dia, stats)
                _ult_run_vnda = _agora()
                from app.models import AppConfig
                AppConfig.set('vnda_ultimo_sync', _ult_run_vnda.isoformat())
                db.session.commit()
            except Exception:
                logger.exception('vnda auto-sync falhou')
            finally:
                if is_pg:
                    try:
                        conn.execute(text('SELECT pg_advisory_unlock(:k)'),
                                     {'k': LOCK_KEY_VNDA})
                    except Exception:
                        pass
        finally:
            conn.close()


def _run_vigia_abandono(app):
    """Job: detecta conversas em status `pending` paradas ha mais de N min e
    chama o vigia pra avaliar abandono. Anti-spam por set em memoria."""
    from app.services import chatbot_vigia, chatwoot

    with app.app_context():
        if not chatbot_vigia.disponivel() or not chatwoot.bot_disponivel():
            return
        uri = app.config.get('SQLALCHEMY_DATABASE_URI', '') or ''
        is_pg = 'postgresql' in uri
        from app.extensions import db
        conn = db.engine.connect()
        try:
            if is_pg:
                got = conn.execute(text('SELECT pg_try_advisory_lock(:k)'),
                                   {'k': LOCK_KEY_VIGIA_ABANDONO}).scalar()
                if not got:
                    return
            try:
                min_minutos = int(app.config.get('CHATBOT_VIGIA_ABANDONO_MIN', 15) or 15)
                paradas = chatwoot.listar_conversas_paradas(min_minutos=min_minutos)
                for c in paradas:
                    conv_id = c.get('id')
                    if not conv_id or conv_id in chatbot_vigia._avisados_abandono:
                        continue
                    historico = chatwoot.buscar_historico(conv_id)
                    if not historico:
                        continue
                    try:
                        chatbot_vigia.avaliar_abandono(
                            historico, conv_id=conv_id,
                            nome_contato=c.get('nome_contato') or '',
                            minutos_sem_resposta=c.get('minutos_paradas', min_minutos))
                    except Exception:
                        logger.exception('vigia abandono falhou conv=%s', conv_id)
                    # Marca como avisado mesmo se o vigia decidiu silenciar — anti-spam.
                    chatbot_vigia._avisados_abandono.add(conv_id)
            except Exception:
                logger.exception('vigia abandono ciclo falhou')
            finally:
                if is_pg:
                    try:
                        conn.execute(text('SELECT pg_advisory_unlock(:k)'),
                                     {'k': LOCK_KEY_VIGIA_ABANDONO})
                    except Exception:
                        pass
        finally:
            conn.close()


def _run_auditor(app, modo='janela'):
    """Job recorrente do auditor. `modo`:
      - 'janela': audita desde a ultima execucao; manda Z-API SO se anormal.
      - 'resumo': audita o dia inteiro; sempre manda Z-API (fim de dia)."""
    from app.services import chatbot_auditor
    with app.app_context():
        if not chatbot_auditor.disponivel():
            return
        uri = app.config.get('SQLALCHEMY_DATABASE_URI', '') or ''
        is_pg = 'postgresql' in uri
        from app.extensions import db
        conn = db.engine.connect()
        try:
            if is_pg:
                got = conn.execute(text('SELECT pg_try_advisory_lock(:k)'),
                                   {'k': LOCK_KEY_AUDITOR}).scalar()
                if not got:
                    return
            try:
                if modo == 'resumo':
                    r = chatbot_auditor.auditar_dia_resumo(enviar=True)
                else:
                    r = chatbot_auditor.auditar_janela_pendente(enviar=True)
                logger.info('auditor %s rodou: enviado=%s pulou=%s erro=%s',
                            modo, r.get('enviado'), r.get('pulou'),
                            r.get('erro'))
            except Exception:
                logger.exception('auditor %s falhou', modo)
            finally:
                if is_pg:
                    try:
                        conn.execute(text('SELECT pg_advisory_unlock(:k)'),
                                     {'k': LOCK_KEY_AUDITOR})
                    except Exception:
                        pass
        finally:
            conn.close()


def iniciar(app):
    """Inicia o scheduler. Chamado uma vez no startup do app.
    Roda jobs Seru E VNDA no mesmo scheduler."""
    global _scheduler
    if _scheduler is not None:
        return
    if os.environ.get('SERU_AUTO_SYNC', '1') == '0':
        logger.info('Auto-sync DESLIGADO (SERU_AUTO_SYNC=0)')
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        logger.warning('APScheduler nao instalado — auto-sync DESLIGADO')
        return

    _scheduler = BackgroundScheduler(daemon=True, timezone='America/Sao_Paulo')
    _scheduler.add_job(
        lambda: _run_sync(app),
        'interval', minutes=15, id='seru-sync',
        max_instances=1, coalesce=True,
    )
    _scheduler.add_job(
        lambda: _run_vnda_sync(app),
        'interval', minutes=15, id='vnda-sync',
        max_instances=1, coalesce=True,
    )

    # Resumo diario de pedidos no Slack as 04:00 BRT
    _scheduler.add_job(
        lambda: _run_slack_resumo_diario(app),
        'cron', hour=4, minute=0, id='slack-resumo-diario',
        max_instances=1, coalesce=True,
    )

    # Lembretes de pedido pra amanha — 4 vezes ao dia
    for h in (9, 12, 16, 19):
        _scheduler.add_job(
            lambda app=app: _run_slack_lembretes_amanha(app),
            'cron', hour=h, minute=0, id=f'slack-lembrete-{h}h',
            max_instances=1, coalesce=True,
        )

    # Lembrete de pedidos de HOJE nao entregues — 10h, 11h, ..., 19h
    _scheduler.add_job(
        lambda app=app: _run_slack_lembretes_pedidos_hoje(app),
        'cron', hour='10-19', minute=0, id='slack-lembrete-pedidos-hoje',
        max_instances=1, coalesce=True,
    )

    # Digest WhatsApp de tarefas do dia (PARA) — 07:00 BRT
    _scheduler.add_job(
        lambda app=app: _run_zapi_digest_tarefas(app),
        'cron', hour=7, minute=0, id='zapi-digest-tarefas',
        max_instances=1, coalesce=True,
    )

    # Radar de saude do negocio (contas a pagar + receitas) — 07:30 BRT.
    # Desligar: DIGEST_SAUDE=0.
    if os.environ.get('DIGEST_SAUDE', '1') != '0':
        _scheduler.add_job(
            lambda app=app: _run_zapi_digest_saude(app),
            'cron', hour=7, minute=30, id='zapi-digest-saude',
            max_instances=1, coalesce=True,
        )

    # Digest WhatsApp de anomalias do dia — 23:00 BRT (apos fechamento)
    _scheduler.add_job(
        lambda app=app: _run_zapi_digest_anomalias(app),
        'cron', hour=23, minute=0, id='zapi-digest-anomalias',
        max_instances=1, coalesce=True,
    )

    # Vigia de infra do Chatwoot — 15 em 15 min. Desligar: CHATWOOT_VIGIA_INFRA=0.
    if os.environ.get('CHATWOOT_VIGIA_INFRA', '1') != '0':
        _scheduler.add_job(
            lambda app=app: _run_vigia_chatwoot(app),
            'interval', minutes=15, id='chatwoot-vigia-infra',
            max_instances=1, coalesce=True,
        )

    # Alerta de desperdicio: escalada Slack (20:10/15/20/25) -> WhatsApp (20:30)
    if os.environ.get('DESPERDICIO_ALERTA', '1') != '0':
        # 4 ticks no Slack — gerentes veem la e podem resolver antes do WhatsApp
        for minuto in (10, 15, 20, 25):
            _scheduler.add_job(
                lambda app=app: _run_desperdicio_alerta_slack(app),
                'cron', hour=20, minute=minuto,
                id=f'slack-desperdicio-alerta-{minuto}',
                max_instances=1, coalesce=True,
            )
        # Escalada final no WhatsApp se ainda houver pendentes as 20:30
        _scheduler.add_job(
            lambda app=app: _run_desperdicio_alerta(app),
            'cron', hour=20, minute=30, id='zapi-desperdicio-alerta',
            max_instances=1, coalesce=True,
        )

    # Backup do Postgres pro Dropbox — 04:00 BRT diario
    if os.environ.get('BACKUP_AUTO', '1') != '0':
        _scheduler.add_job(
            lambda app=app: _run_backup_diario(app),
            'cron', hour=4, minute=0, id='backup-diario',
            max_instances=1, coalesce=True,
        )

    # Backup do Postgres do Chatwoot (banco separado) — 04:20 BRT diario.
    # So agenda se houver CHATWOOT_DATABASE_URL e BACKUP_CHATWOOT != 0.
    if (os.environ.get('BACKUP_CHATWOOT', '1') != '0'
            and (app.config.get('CHATWOOT_DATABASE_URL') or '').strip()):
        _scheduler.add_job(
            lambda app=app: _run_backup_chatwoot(app),
            'cron', hour=4, minute=20, id='backup-chatwoot',
            max_instances=1, coalesce=True,
        )

    # Cache de pedidos do site (VNDA) pro card de cliente do CRM/Chatwoot —
    # janela curta a cada 1h (going-forward). So agenda se houver token VNDA.
    # Desligavel via env VNDA_CARD_SYNC=0. Historico antigo: botao manual em
    # /pdv/vnda/mapeamentos.
    if (os.environ.get('VNDA_CARD_SYNC', '1') != '0'
            and (app.config.get('VNDA_API_TOKEN') or '').strip()):
        _scheduler.add_job(
            lambda app=app: _run_vnda_card_sync(app),
            'cron', minute=0, id='vnda-card-sync',
            max_instances=1, coalesce=True,
        )

    # Automacoes WhatsApp configuraveis (mensagens agendadas) — checa a cada 5 min
    _scheduler.add_job(
        lambda app=app: _run_automacoes_whatsapp(app),
        'cron', minute='*/5', id='automacoes-whatsapp',
        max_instances=1, coalesce=True,
    )

    # Auditor proativo do chatbot — HIBRIDO:
    #   07, 09, 12, 15h BRT: 'janela' — audita janela curta desde a ultima
    #     execucao; manda WhatsApp SO se houver anormalidade.
    #   19h BRT: 'resumo' — audita o dia inteiro; SEMPRE manda WhatsApp (com
    #     numeros, insights e problemas, mesmo se foi tranquilo).
    # Desligavel via CHATBOT_AUDITOR_AUTO=0.
    if os.environ.get('CHATBOT_AUDITOR_AUTO', '1') != '0':
        for h in (7, 9, 12, 15):
            _scheduler.add_job(
                lambda app=app: _run_auditor(app, modo='janela'),
                'cron', hour=h, minute=0, id=f'chatbot-auditor-{h}h',
                max_instances=1, coalesce=True,
            )
        _scheduler.add_job(
            lambda app=app: _run_auditor(app, modo='resumo'),
            'cron', hour=19, minute=0, id='chatbot-auditor-19h-resumo',
            max_instances=1, coalesce=True,
        )

    # Vigia do chatbot — detector de conversas ABANDONADAS. Roda a cada 5 min,
    # acha conversas em status `pending` paradas ha > CHATBOT_VIGIA_ABANDONO_MIN
    # (default 15 min) e avalia via Haiku. Anti-spam: uma conversa nao recebe
    # 2 avisos de abandono na mesma sessao do app.
    _scheduler.add_job(
        lambda app=app: _run_vigia_abandono(app),
        'cron', minute='*/5', id='vigia-abandono',
        max_instances=1, coalesce=True,
    )

    _scheduler.start()
    logger.info('Auto-sync iniciado: Seru + VNDA 15min · resumo 04:00 · lembretes amanha 9h/12h/16h/19h · pedidos hoje 10-19h · zapi tarefas 07:00 · zapi anomalias 23:00 · desperdicio slack 20:10/15/20/25 + whatsapp 20:30 · backup 04:00 · automacoes whatsapp 5min')


def _run_slack_resumo_diario(app):
    """Job: posta o resumo de pedidos do dia no Slack (04:00 BRT).

    Usa pg_advisory_lock pra garantir 1x entre workers gunicorn.
    """
    from app.services import slack_resumos

    with app.app_context():
        _com_lock(7725, slack_resumos.enviar_resumo_pedidos_dia, 'slack resumo diario')


def _run_slack_lembretes_amanha(app):
    """Job: posta lembretes pra lojas sem pedido pra amanha (9/12/16/19h BRT)."""
    from app.services import slack_resumos

    with app.app_context():
        _com_lock(7726, slack_resumos.enviar_lembretes_pedido_amanha,
                  'slack lembrete pedido amanha')


def _run_zapi_digest_tarefas(app):
    """Job: envia digest WhatsApp das tarefas do dia (07:00 BRT)."""
    from app.services import zapi_resumos

    with app.app_context():
        _com_lock(7728, zapi_resumos.enviar_digest_tarefas, 'zapi digest tarefas')


def _run_zapi_digest_saude(app):
    """Job: radar de saude do negocio (contas a pagar + receitas), 07:30 BRT."""
    from app.services import saude_negocio

    with app.app_context():
        _com_lock(7736, saude_negocio.enviar_digest_saude, 'zapi digest saude')


def _run_zapi_digest_anomalias(app):
    """Job: envia digest WhatsApp de anomalias do dia (23:00 BRT)."""
    from app.services import anomalias

    with app.app_context():
        _com_lock(7730, anomalias.enviar_digest_whatsapp, 'zapi digest anomalias')


def _run_desperdicio_alerta_slack(app):
    """Job: posta no Slack as lojas sem desperdicio lancado (20:10/15/20/25 BRT).

    Cada tick re-consulta o banco — lojas que lancarem entre os ticks somem
    do proximo lembrete. Sem envio se nao houver pendentes.
    """
    from app.services import desperdicio_alerta

    with app.app_context():
        _com_lock(7734, desperdicio_alerta.alertar_slack_pendentes,
                  'slack alerta desperdicio')


def _run_desperdicio_alerta(app):
    """Job: escalada final no WhatsApp as 20:30 BRT — se ainda houver lojas
    sem desperdicio lancado depois dos 4 lembretes no Slack."""
    from app.services import desperdicio_alerta

    with app.app_context():
        _com_lock(7733, desperdicio_alerta.enviar_alerta_desperdicio,
                  'zapi alerta desperdicio')


def _run_automacoes_whatsapp(app):
    """Job: dispara as automacoes WhatsApp agendadas que estao no horario
    (checa a cada 5 min). Idempotente por dia via ultimo_disparo_em."""
    from app.services import whatsapp

    with app.app_context():
        _com_lock(7732, whatsapp.disparar_automacoes_devidas, 'automacoes whatsapp')



def _run_vigia_chatwoot(app):
    """Job: vigia de infra do Chatwoot (15 em 15 min). Criado em
    12/06/2026 apos a equipe de atendimento descobrir o Chatwoot quebrado
    antes do sistema. Alerta o dono no WhatsApp na transicao
    saudavel→doente (anti-spam de 6h dentro do servico)."""
    from app.services import chatwoot

    with app.app_context():
        _com_lock(LOCK_KEY_VIGIA_CHATWOOT, chatwoot.vigiar_infra,
                  'vigia infra chatwoot')


def _run_backup_diario(app):
    """Job: backup do Postgres pro Dropbox (04:00 BRT). Advisory lock pra
    garantir 1 execucao entre workers gunicorn."""
    from app.extensions import db
    from app.services import backup
    from app.utils import agora as _agora

    def _fn():
        global _ult_run_backup
        resultado = backup.executar_backup()
        _ult_run_backup = _agora()
        if not resultado['ok']:
            logger.warning('backup diario falhou: %s', resultado.get('motivo'))
            return
        # Retencao SO roda apos backup OK: tudo que ela apaga do banco esta
        # no dump de hoje (recuperavel por RETENCAO_BACKUPS_DIAS=90 dias).
        # Backup falhou -> pula a limpeza, sem excecao.
        if app.config.get('RETENCAO_AUTO', True):
            try:
                from app.services import retencao
                retencao.executar_limpeza()
            except Exception:  # noqa: BLE001
                logger.exception('retencao diaria falhou (backup ja esta OK)')

    with app.app_context():
        if db.engine.dialect.name != 'postgresql':
            return  # backup nao roda em SQLite local
        _com_lock(LOCK_KEY_BACKUP, _fn, 'backup diario')


def _run_backup_chatwoot(app):
    """Job: backup do Postgres do CHATWOOT pro Dropbox (04:20 BRT).

    Banco separado do sistema (CHATWOOT_DATABASE_URL). Reusa o mesmo
    servico de backup, mudando a URL alvo e o prefixo/pasta do arquivo.
    Advisory lock proprio (no banco do sistema) pra exec unica entre workers.
    """
    from app.extensions import db
    from app.services import backup
    from app.utils import agora as _agora

    chatwoot_url = (app.config.get('CHATWOOT_DATABASE_URL') or '').strip()
    if not chatwoot_url:
        return

    def _fn():
        global _ult_run_backup_chatwoot
        resultado = backup.executar_backup(
            db_url=chatwoot_url, prefixo='chatwoot', pasta='/backups-chatwoot')
        _ult_run_backup_chatwoot = _agora()
        if not resultado['ok']:
            logger.warning('backup chatwoot falhou: %s', resultado.get('motivo'))

    with app.app_context():
        if db.engine.dialect.name != 'postgresql':
            return  # advisory lock exige Postgres
        _com_lock(LOCK_KEY_BACKUP_CHATWOOT, _fn, 'backup chatwoot')


def _run_vnda_card_sync(app):
    """Job: atualiza o cache de pedidos do site (VNDA) pro card de cliente
    do CRM. Janela curta (going-forward). Advisory lock pra exec unica entre
    workers gunicorn. Falha da API VNDA so loga (nao quebra o scheduler)."""
    from app.extensions import db
    from app.services import vnda_card

    def _fn():
        try:
            r = vnda_card.sincronizar_recentes(dias=3)
            logger.info('vnda card sync: %s', r)
        except Exception as exc:  # noqa: BLE001
            logger.warning('vnda card sync falhou: %s', exc)

    with app.app_context():
        if db.engine.dialect.name != 'postgresql':
            return  # advisory lock exige Postgres
        _com_lock(LOCK_KEY_VNDA_CARD, _fn, 'vnda card sync')


def status_backup():
    """Status do job backup pra UI."""
    return {
        'ativo': _scheduler is not None and _scheduler.running,
        'ultimo_run': _ult_run_backup,
    }


def _run_slack_lembretes_pedidos_hoje(app):
    """Job: posta no #pedidos lembretes de pedidos de hoje nao entregues
    (10h, 11h, ..., 19h BRT). Advisory lock pra evitar duplicacao entre workers."""
    from app.services import slack_resumos

    with app.app_context():
        _com_lock(7727, slack_resumos.enviar_lembrete_pedidos_hoje_pendentes,
                  'slack lembrete pedidos hoje')

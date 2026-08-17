"""Auto-sync Seru → EstoqueLoja (cron 15 minutos).

APScheduler roda dentro de cada worker gunicorn. Pra evitar execucao
duplicada quando ha multiplos workers, usamos pg_try_advisory_lock no
PostgreSQL — so 1 worker pega o lock por vez, os outros pulam.

Ativado por default. Pra desligar em runtime: setar env var
SERU_AUTO_SYNC=0 antes do startup.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

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
_ult_run_backup = None
_ult_run_backup_chatwoot = None
LOCK_KEY = 7723  # advisory lock pro Seru
LOCK_KEY_BACKUP = 7731  # advisory lock pro backup diario
LOCK_KEY_BACKUP_CHATWOOT = 7735  # advisory lock pro backup do banco do Chatwoot
LOCK_KEY_VNDA_CARD = 7736  # advisory lock pro cache de pedidos do site (card CRM)
LOCK_KEY_VIGIA_ABANDONO = 7737  # advisory lock pro detector de conversas abandonadas
LOCK_KEY_AUDITOR = 7738  # advisory lock pro auditor diario do bot
LOCK_KEY_VIGIA_CHATWOOT = 7739  # advisory lock pro vigia de infra do Chatwoot
# 7747: era 7740, mas 7740 colidia com o lock da migracao de schema em
# migrations_legacy._migrate_postgres (deploy escalonado podia PULAR a migracao
# em silencio). Compartilhado com a vassoura do bot por design.
LOCK_KEY_FOLLOWUP = 7747  # advisory lock pro follow-up do bot (cliente sumiu)
LOCK_KEY_RESERVA_EXPIRA = 7742  # advisory lock pro libera-reservas-expiradas da loja online
LOCK_KEY_PREVISAO_ACURACIA = 7743  # advisory lock pro snapshot+match de acuracia do forecast
LOCK_KEY_BAIXAS_PRESAS = 7744  # advisory lock pro alerta de baixas presas (separado/retirada)
LOCK_KEY_SITE_VIGIA = 7745  # advisory lock pro vigia do site (canarios de frete/catalogo/agenda)
LOCK_KEY_PDV_VIGIA = 7746  # advisory lock pro vigia do PDV (loja muda / company sem vinculo)
LOCK_KEY_USO_IA_VIGIA = 7748  # advisory lock pro vigia de custo de IA (teto diario)
LOCK_KEY_GOOGLE_REVIEWS = 7749  # advisory lock pro sync de avaliacoes do Google
# 7752 = LOCK_KEY_REPROCESSO (seru_sync.py) — reprocesso retroativo de baixas.
# 7753 = reservado pro chatbot (lock por conversa cross-worker).
LOCK_KEY_TREINO_FECH = 7754    # advisory lock pro fechamento semanal do treino
LOCK_KEY_TREINO_DIARIO = 7755  # advisory lock pros jobs diarios do treino
LOCK_KEY_TINY_PDV = 7756  # advisory lock pro import do PDV do Tiny (Cantina)
# 7757 RESERVADO: acerto de despacho direto (acerto_despacho.py)
LOCK_KEY_AUTO_PEDIDOS = 7758  # advisory lock pros pedidos automaticos loja->industria
LOCK_KEY_AUTO_ENVIO = 7759  # advisory lock pras ordens da SEMANA (dom 12:00 + rede diaria + retro)
LOCK_KEY_DIGEST_RECEBIMENTOS = 7760  # advisory lock pro digest 12:00 de pedidos recebidos
LOCK_KEY_ATUALIZA_PLANO = 7761  # advisory lock pro 🔄 automatico da ordem do dia (06:45/19:05)
# 7750 foi reciclado: era do `briefing-dono` (removido 17/07/2026), agora e do
# marketing (sync da base + campanha de aniversario no Listmonk).
LOCK_KEY_MARKETING = 7750  # advisory lock pro marketing (Listmonk)
# Locks LIBERADOS mas RESERVADOS (nao reusar — evita conflito se algum
# dos jobs for reativado no futuro):
# - 7730 era do `zapi-digest-anomalias` (job 23:00 BRT, removido 14/06/2026).
# - 7741 era do `zapi-digest-saude` (job 07:30 BRT, removido 14/06/2026).
# - 7750 era do `briefing-dono` (job 07:00 BRT, removido 17/07/2026 a pedido
#   do dono; o envio manual em /admin/briefing nao usa lock).


def _erro_transitorio(exc):
    """Condições ESPERADAS e re-tentáveis do cron que NÃO devem virar evento
    no Sentry (17/08/2026 — a integração de logging promove todo ERROR a
    evento e a cota grátis estourou só com este ruído):

    - RuntimeError de SHUTDOWN do interpretador: deploy no meio do ciclo —
      o ThreadPoolExecutor do `listar_pedidos_completo` recusa submissões
      ("cannot schedule new futures..."). O worker novo refaz o ciclo.
    - Falha de REDE da API da Seru que sobrou APÓS os retries do `_get`
      (pool/ConnectionError, "Response ended prematurely", timeout, 5xx do
      gateway). O ciclo de 15min re-tenta e o catch-up cobre o buraco;
      indisponibilidade PERSISTENTE aparece pelos vigias/snapshot, não por
      evento de exceção.

    Qualquer outra exceção continua como `logger.exception` → Sentry.
    Justificativa documentada aqui de propósito (regra do CLAUDE.md:
    nunca silenciar erro sem justificativa)."""
    import requests as _rq

    from app.services import seru as _seru
    if isinstance(exc, _seru._Erro5xx):
        return True
    if isinstance(exc, RuntimeError) and 'shutdown' in str(exc).lower():
        return True
    if isinstance(exc, _rq.exceptions.RequestException):
        return True
    return False


def _falha_de_job(label, exc):
    """Log canônico de falha de job do cron: transitório vira WARNING (não
    gera evento Sentry), o resto segue exception/ERROR."""
    if _erro_transitorio(exc):
        logger.warning('%s: condição transitória (rede Seru/shutdown de '
                       'deploy) — próximo ciclo re-tenta: %s',
                       label, str(exc)[:200])
    else:
        logger.exception('%s falhou', label)


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
        except Exception as exc:  # noqa: BLE001 — _falha_de_job classifica
            _falha_de_job(label, exc)
        return
    conn = db.engine.connect()
    try:
        got = bool(conn.execute(text('SELECT pg_try_advisory_lock(:k)'),
                                {'k': key}).scalar())
        if not got:
            return
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — _falha_de_job classifica
            _falha_de_job(label, exc)
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
                # Snapshot persistente das vendas (VendaSeruDiaria) pro relatorio/
                # XLSX lerem sem re-consultar a API — e resiste a API fora na hora
                # do relatorio. Best-effort: nunca derruba o sync de estoque.
                try:
                    from app.services import vendas_diarias
                    vendas_diarias.capturar_periodo(hoje - timedelta(days=1), hoje)
                except Exception as exc:  # noqa: BLE001 — _falha_de_job classifica
                    _falha_de_job('captura vendas_diarias no cron', exc)
                    # Sem rollback, os DELETEs pendentes de um snapshot que
                    # falhou no meio seriam COMMITADOS pelo proximo commit da
                    # mesma sessao (ex: o do vigia abaixo) — dias sumiriam
                    # dos relatorios ate o proximo ciclo (achado de revisao).
                    try:
                        db.session.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                # Retoma reprocesso retroativo orfao (drenador morto em
                # deploy / erro de API na tentativa anterior). No-op sem
                # pendencia. Best-effort: nunca derruba o sync.
                try:
                    seru_sync.retomar_reprocesso_pendente(app)
                except Exception as exc:  # noqa: BLE001 — _falha_de_job classifica
                    _falha_de_job('retomada de reprocesso pendente', exc)
                # Vigia de venda SEM itens (18/07/2026, caso Nebraska: 23
                # cobrancas "PDV Facil" so-valor, R$7.028,50, sem NF). Roda
                # DENTRO do advisory lock do sync — execucao unica entre
                # workers, sem alerta duplicado. Best-effort. Desligar:
                # VENDA_SEM_ITEM_VIGIA=0.
                try:
                    if os.environ.get('VENDA_SEM_ITEM_VIGIA', '1') != '0':
                        from app.services import venda_sem_item_vigia
                        venda_sem_item_vigia.vigiar()
                except Exception as exc:  # noqa: BLE001 — _falha_de_job classifica
                    _falha_de_job('vigia venda sem item', exc)
                # Estorno que nunca vai disparar (cancelamento SEM
                # canceledAt): o `processar_pedidos` ja detectou e poe em
                # stats; aqui so avisa. NAO mexe em estoque — decisao do
                # dono 26/07/2026 ("alertar", nao "corrigir o gatilho").
                try:
                    pend = stats.get('estornos_pendentes') or []
                    if pend:
                        from app.services import estorno_pendente_vigia
                        estorno_pendente_vigia.alertar(pend)
                except Exception:
                    logger.exception('vigia estorno pendente falhou')
                ativas = any(stats.get(k, 0) for k in (
                    'pedidos_novos', 'itens_baixados',
                    'pedidos_cancelados_estornados'))
                if ativas:
                    logger.info('seru auto-sync (com mudancas): %s', stats)
                else:
                    logger.debug('seru auto-sync (sem mudancas)')
            except Exception as exc:  # noqa: BLE001 — _falha_de_job classifica
                _falha_de_job('seru auto-sync', exc)
            finally:
                if is_pg:
                    try:
                        conn.execute(text('SELECT pg_advisory_unlock(:k)'),
                                     {'k': LOCK_KEY})
                    except Exception:
                        pass
        finally:
            conn.close()


def _run_vigia_abandono(app):
    """Job: detecta conversas em status `pending` paradas ha mais de N min e
    chama o vigia pra avaliar abandono.

    3 freios (calibrados no caso real de 12/06/2026, quando o detector —
    recem-curado da cegueira do token — metralhou o dono com o backlog
    inteiro de uma vez):
    1. Dedupe PERSISTENTE: `ja_avisado_abandono` consulta VigiaVeredito
       no banco (sobrevive a deploy; o set em memoria zerava a cada um).
    2. Idade maxima (`CHATBOT_VIGIA_ABANDONO_MAX_MIN`, default 720 =
       12h): conversa fria de ontem nao e 'abandono em andamento' — vira
       ruido as 15h do dia seguinte.
    3. Teto por ciclo (`CHATBOT_VIGIA_ABANDONO_MAX_POR_CICLO`, default
       5): backlog grande escoa ao longo dos ciclos de 15min em vez de
       virar rajada no WhatsApp."""
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
                max_minutos = int(app.config.get(
                    'CHATBOT_VIGIA_ABANDONO_MAX_MIN', 720) or 720)
                max_por_ciclo = int(app.config.get(
                    'CHATBOT_VIGIA_ABANDONO_MAX_POR_CICLO', 5) or 5)
                paradas = chatwoot.listar_conversas_paradas(min_minutos=min_minutos)
                avaliadas = 0
                for c in paradas:
                    if avaliadas >= max_por_ciclo:
                        break
                    conv_id = c.get('id')
                    if not conv_id:
                        continue
                    if c.get('minutos_paradas', 0) > max_minutos:
                        continue   # conversa fria — nao e abandono em andamento
                    if chatbot_vigia.ja_avisado_abandono(conv_id):
                        continue
                    historico = chatwoot.buscar_historico(conv_id)
                    if not historico:
                        continue
                    try:
                        chatbot_vigia.avaliar_abandono(
                            historico, conv_id=conv_id,
                            nome_contato=c.get('nome_contato') or '',
                            minutos_sem_resposta=c.get('minutos_paradas', min_minutos))
                        avaliadas += 1
                    except Exception:
                        logger.exception('vigia abandono falhou conv=%s', conv_id)
                    # Marca como avisado mesmo se o vigia decidiu silenciar — anti-spam.
                    chatbot_vigia._avisados_abandono.add(conv_id)
                # Detector C (12/06/2026, conv #198): cliente esperando
                # ATENDENTE em conversa `open` — invisivel pro bot (que
                # ignora open por design) e pro detector de abandono
                # (que so olha pending). Deterministico, dedupe proprio,
                # mesmo ciclo/lock. Desligar: CHATBOT_VIGIA_ESPERA=0.
                if str(app.config.get('CHATBOT_VIGIA_ESPERA',
                                      os.environ.get('CHATBOT_VIGIA_ESPERA',
                                                     '1'))) != '0':
                    try:
                        min_espera = int(app.config.get(
                            'CHATBOT_VIGIA_ESPERA_MIN', 10) or 10)
                        chatbot_vigia.alertar_clientes_esperando_humano(
                            min_minutos=min_espera)
                    except Exception:
                        logger.exception('vigia espera-humano ciclo falhou')
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


def _treino_sob_lock(app, lock_key, fn, nome):
    """Roda um job do treino sob advisory lock (execucao unica entre workers).
    Desligavel via TREINO_JOBS=0. Best-effort — nunca derruba o scheduler."""
    if os.environ.get('TREINO_JOBS', '1') == '0':
        return
    with app.app_context():
        uri = app.config.get('SQLALCHEMY_DATABASE_URI', '') or ''
        is_pg = 'postgresql' in uri
        from app.extensions import db
        conn = db.engine.connect()
        try:
            if is_pg:
                got = conn.execute(text('SELECT pg_try_advisory_lock(:k)'),
                                   {'k': lock_key}).scalar()
                if not got:
                    return
            try:
                res = fn()
                logger.info('treino job %s: %s', nome, res)
            except Exception:
                logger.exception('treino job %s falhou', nome)
            finally:
                if is_pg:
                    try:
                        conn.execute(text('SELECT pg_advisory_unlock(:k)'),
                                     {'k': lock_key})
                    except Exception:
                        pass
        finally:
            conn.close()


def _run_treino_fechamento(app):
    """Fechamento semanal do treino (domingo 23:55): meta, streaks e marcos."""
    from app.services import treino_jobs
    from app.utils import hoje

    def _fn():
        ano, semana, _ = hoje().isocalendar()
        return f'{treino_jobs.fechamento_semanal(ano, semana)} func processado(s)'
    _treino_sob_lock(app, LOCK_KEY_TREINO_FECH, _fn, 'fechamento-semanal')


def _run_treino_diario(app):
    """Jobs diarios do treino: snapshot do ranking, encerramento de temporada
    vencida e limpeza de tentativas de quiz abandonadas."""
    from app.services import treino_jobs

    def _fn():
        s = treino_jobs.snapshot_ranking()
        e = treino_jobs.encerramento_temporada()
        li = treino_jobs.limpeza_tentativas()
        return f'{s} unidade(s) no ranking, {e} temporada(s) encerrada(s), ' \
               f'{li} tentativa(s) finalizada(s)'
    _treino_sob_lock(app, LOCK_KEY_TREINO_DIARIO, _fn, 'diario')


def _run_snapshot_acuracia(app):
    """Job diario de acuracia do forecast: congela a previsao do pedido
    semanal (idempotente) e casa o realizado das datas que ja passaram.
    Desligavel via PREVISAO_ACURACIA=0."""
    if os.environ.get('PREVISAO_ACURACIA', '1') == '0':
        return
    from app.services import previsao_acuracia
    with app.app_context():
        uri = app.config.get('SQLALCHEMY_DATABASE_URI', '') or ''
        is_pg = 'postgresql' in uri
        from app.extensions import db
        conn = db.engine.connect()
        try:
            if is_pg:
                got = conn.execute(text('SELECT pg_try_advisory_lock(:k)'),
                                   {'k': LOCK_KEY_PREVISAO_ACURACIA}).scalar()
                if not got:
                    return
            try:
                novos = previsao_acuracia.registrar_snapshot()
                casados = previsao_acuracia.casar_realizados()
                logger.info('acuracia forecast: %d snapshot(s) novo(s), '
                            '%d casado(s)', novos, casados)
            except Exception:
                logger.exception('snapshot de acuracia do forecast falhou')
            finally:
                if is_pg:
                    try:
                        conn.execute(text('SELECT pg_advisory_unlock(:k)'),
                                     {'k': LOCK_KEY_PREVISAO_ACURACIA})
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
    # VNDA APOSENTADO em 24/06/2026 (operacao 100% no sistema proprio). A baixa
    # de venda do VNDA foi REMOVIDA do codigo (motor unico em baixa_venda.py).
    # O cliente API (vnda.py), o card CRM (vnda_card.py) e a rota de contatos
    # seguem vivos pra historico.

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

    # Treinamento gamificado (§13). Desligar: TREINO_JOBS=0.
    # Fechamento semanal (meta + streaks) — domingo 23:55 BRT.
    _scheduler.add_job(
        lambda app=app: _run_treino_fechamento(app),
        'cron', day_of_week='sun', hour=23, minute=55, id='treino-fechamento',
        max_instances=1, coalesce=True,
    )
    # Diario 23:50 BRT: snapshot do ranking + encerra temporada vencida +
    # limpa tentativas de quiz abandonadas.
    _scheduler.add_job(
        lambda app=app: _run_treino_diario(app),
        'cron', hour=23, minute=50, id='treino-diario',
        max_instances=1, coalesce=True,
    )

    # Radar de saude do negocio (Contas a Pagar + Receitas):
    # DESLIGADO em 14/06/2026 por pedido do dono — chegava todo dia 07:30
    # BRT no WhatsApp dele. A rota `/admin/saude` continua disponivel
    # pra consulta manual quando o dono quiser. NAO reativar como cron
    # automatico sem decisao explicita do dono. Servico `saude_negocio.py`
    # e funcao `_run_zapi_digest_saude` mantidos pro uso da rota.

    # Digest WhatsApp "Alertas do dia" (vendas atipicas + quedas + estoque
    # parado): DESLIGADO em 14/06/2026 por pedido do dono — chegava todo dia
    # 23:00 BRT no WhatsApp dele. A funcao `anomalias.enviar_digest_whatsapp`
    # continua viva pra rota admin (`/notificacoes/...`) e pra tool do
    # copilot (`enviar_digest_whatsapp`) — o dono ainda pode pedir o
    # resumo sob demanda. NAO reativar como cron sem decisao explicita.

    # Vigia de infra do Chatwoot — 15 em 15 min. Desligar: CHATWOOT_VIGIA_INFRA=0.
    if os.environ.get('CHATWOOT_VIGIA_INFRA', '1') != '0':
        _scheduler.add_job(
            lambda app=app: _run_vigia_chatwoot(app),
            'interval', minutes=15, id='chatwoot-vigia-infra',
            max_instances=1, coalesce=True,
        )

    # Vigia do SITE (05/07/2026, pedido do dono no dia do incidente do
    # frete): canarios de frete/catalogo/agenda a cada 2h, alerta WhatsApp
    # na transicao. 2h (nao 15min) por causa dos geocoders externos
    # (Nominatim rate-limita). Desligar: SITE_VIGIA=0.
    if os.environ.get('SITE_VIGIA', '1') != '0':
        _scheduler.add_job(
            lambda app=app: _run_site_vigia(app),
            'interval', hours=2, id='site-vigia',
            max_instances=1, coalesce=True,
        )

    # Vigia do PDV (07/07/2026, pedido do dono apos o incidente da Ribeiro:
    # renome no Seru deixou a loja 2 SEMANAS sem baixar venda, em silencio).
    # Canarios: sync parado, loja confirmada que vendia e ficou muda (36h),
    # company vendendo sem vinculo confirmado. Alerta WhatsApp na transicao.
    # Desligar: PDV_VIGIA=0.
    if os.environ.get('PDV_VIGIA', '1') != '0':
        _scheduler.add_job(
            lambda app=app: _run_pdv_vigia(app),
            'interval', minutes=30, id='pdv-vigia',
            max_instances=1, coalesce=True,
        )

    # Vigia de CUSTO de IA (11/07/2026): o gasto do dia (UsoIA) passou do
    # teto USO_IA_TETO_DIA_USD? Alerta WhatsApp na transicao (anti-spam de
    # 6h no servico). O relatorio /admin/uso-ia e passivo — sem este job,
    # um loop de bot dispararia custo em silencio. Desligar: USO_IA_VIGIA=0.
    if os.environ.get('USO_IA_VIGIA', '1') != '0':
        _scheduler.add_job(
            lambda app=app: _run_uso_ia_vigia(app),
            'interval', hours=1, id='uso-ia-vigia',
            max_instances=1, coalesce=True,
        )

    # Avaliacoes do Google (12/07/2026): sync de reviews + alerta WhatsApp de
    # review nova (prioriza nota baixa). Intervalo de 1h (API externa, volume
    # baixo). Dormente ate o OAuth+aprovacao do Google. Desligar:
    # GOOGLE_REVIEWS_SYNC=0.
    if os.environ.get('GOOGLE_REVIEWS_SYNC', '1') != '0':
        _scheduler.add_job(
            lambda app=app: _run_google_reviews(app),
            'interval', hours=1, id='google-reviews',
            max_instances=1, coalesce=True,
        )

    # Baixas presas (03/07/2026): pedido parado em 'separado' com entrega
    # vencida (QR de saida nao escaneado = industria NAO baixou) e retirada
    # de sobra presa em transporte (loja baixou, industria nao creditada).
    # WhatsApp do dono com dedup de 6h. Desligar: ALERTA_BAIXAS_PRESAS=0.
    if os.environ.get('ALERTA_BAIXAS_PRESAS', '1') != '0':
        _scheduler.add_job(
            lambda app=app: _run_alerta_baixas_presas(app),
            'interval', minutes=30, id='alerta-baixas-presas',
            max_instances=1, coalesce=True,
        )

    # Briefing diario do dono: o envio automatico das 07:00 foi REMOVIDO em
    # 17/07/2026 a pedido do dono ("nao quero receber") — menos de 1 dia
    # depois de criado. O servico briefing_dono segue vivo alimentando o
    # bloco "Precisa de voce hoje" da home e a pagina /admin/briefing
    # (preview + envio manual). NAO reagendar sem ordem explicita.

    # Heartbeat invertido — 08:00 BRT (manha): canal Slack recebe um
    # 'sistema OK'. Detecta dependencia circular: se Z-API cair, ninguem
    # avisa o dono via WhatsApp; mas se a msg sumir do Slack, o dono
    # sabe que a infra de alertas caiu. Sem env SLACK_CHANNEL_HEARTBEAT,
    # job nao posta (silencioso por design).
    _scheduler.add_job(
        lambda app=app: _run_heartbeat_slack(app),
        'cron', hour=8, minute=0, id='heartbeat-slack',
        max_instances=1, coalesce=True,
    )

    # Acuracia do forecast — congela a previsao do dia (idempotente) + casa o
    # realizado das datas passadas. 05:30 BRT. Desligar: PREVISAO_ACURACIA=0.
    _scheduler.add_job(
        lambda app=app: _run_snapshot_acuracia(app),
        'cron', hour=5, minute=30, id='previsao-acuracia',
        max_instances=1, coalesce=True,
    )

    # Pedidos AUTOMATICOS loja->industria (10/08/2026; janela virou a
    # SEMANA inteira em 17/08/2026 — "os pedidos da semana tambem devem ser
    # lancados tudo no domingo meio dia"): o job do meio-dia (ordens-semana,
    # abaixo) abre os pedidos de seg..dom; estas 2 rodadas diarias sao o
    # REFRESH da mesma janela (amanha..proximo domingo) — 06:30
    # (planejamento) e 18:30 (venda do dia via estoque atual, 30min antes
    # do corte de 19:00). Pedido tocado por humano NUNCA e sobrescrito;
    # D+1 sob corte nunca e tocado. Desligavel por AUTO_PEDIDOS=0.
    if os.environ.get('AUTO_PEDIDOS', '1') != '0':
        _scheduler.add_job(
            lambda app=app: _run_auto_pedidos(app),
            'cron', hour='6,18', minute=30, id='auto-pedidos',
            max_instances=1, coalesce=True,
        )

    # SEMANA no meio-dia (dono 17/08/2026, "quanto menos e mais"): DOMINGO
    # 12:00 solta os PEDIDOS loja->industria de seg..dom (motor
    # venda+estoque) e em seguida as ORDENS de producao da semana — o
    # padeiro enxerga a semana inteira de uma vez, com o firme ja criado.
    # O job roda TODO dia ao meio-dia DE PROPOSITO: fora do domingo
    # re-sincroniza pedidos/ordens do cron com a realidade e re-preenche
    # buraco (dia excluido, disparo engolido por deploy — o APScheduler nao
    # persiste misfire e o auto-deploy reinicia o processo a qualquer
    # hora). SUBSTITUI o envio diario das 19:00 (10-17/08/2026) — o numero
    # final da ordem DE HOJE segue saindo do 🔄 das 06:45/19:05.
    # Desligavel por AUTO_ENVIO_PLANO=0 (a parte de pedidos respeita
    # AUTO_PEDIDOS=0).
    if os.environ.get('AUTO_ENVIO_PLANO', '1') != '0':
        _scheduler.add_job(
            lambda app=app: _run_ordens_semana(app),
            'cron', hour=12, minute=0, id='ordens-semana',
            max_instances=1, coalesce=True,
        )
        # One-shot de retroacao (17/08/2026): a 1ª semana sai ~2min apos o
        # boot; o marker AppConfig faz os deploys seguintes pularem.
        _scheduler.add_job(
            lambda app=app: _run_ordens_semana_retro(app),
            'date', run_date=datetime.now(timezone.utc) + timedelta(
                seconds=120),
            id='ordens-semana-retro',
        )
        # 🔄 AUTOMATICO da ordem DE HOJE (17/08/2026, caso do 1o fim de
        # semana): os itens de vespera da ordem (levain/lead-1) sao
        # dirigidos pela demanda de AMANHA, que o cron de pedidos
        # re-sincroniza 06:30/18:30 DEPOIS de a ordem congelar as 19:00 da
        # vespera — sem este refresh a ordem amanhecia magra (3 itens vs 8
        # no grid). 06:45 = pos-refresh da manha; 19:05 = pos-corte (numero
        # final pra madrugada). Ordem enviada por HUMANO nunca e tocada.
        # Mesmo kill-switch do envio (e a mesma automacao).
        _scheduler.add_job(
            lambda app=app: _run_atualiza_plano(app),
            'cron', hour=6, minute=45, id='auto-atualiza-plano-manha',
            max_instances=1, coalesce=True,
        )
        _scheduler.add_job(
            lambda app=app: _run_atualiza_plano(app),
            'cron', hour=19, minute=5, id='auto-atualiza-plano-corte',
            max_instances=1, coalesce=True,
        )

    # Import do PDV do TINY (Cantina, 27/07/2026): a cada 15 min, janela
    # ontem+hoje. Desligavel por TINY_PDV_SYNC=0. So roda com a loja
    # configurada em AppConfig `tiny_pdv_loja_id` (o job checa e sai).
    if os.environ.get('TINY_PDV_SYNC', '1') != '0':
        _scheduler.add_job(
            lambda app=app: _run_tiny_pdv(app),
            'interval', minutes=15, id='tiny-pdv-sync',
            max_instances=1, coalesce=True,
        )

    # Digest dos pedidos recebidos nas lojas — 12:00 BRT, UMA mensagem
    # (14/08/2026; o aviso por pedido na hora da entrega foi desligado).
    # Mesmo kill-switch do aviso antigo: ZAPI_BOT_AVISO_RECEBIMENTO=0.
    if os.environ.get('ZAPI_BOT_AVISO_RECEBIMENTO', '1') != '0':
        _scheduler.add_job(
            lambda app=app: _run_digest_recebimentos(app),
            'cron', hour=12, minute=0, id='zapi-digest-recebimentos',
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

    # VNDA card-sync APOSENTADO em 24/06/2026 (junto com o VNDA principal —
    # ver bloco acima). NAO eh mais agendado.

    # Marketing por e-mail (Listmonk) — 09:00 BRT: sincroniza a base e monta a
    # campanha de aniversario do dia. So DISPARA se o dono ligou o automatico
    # em /admin/marketing. Desligar de vez: MARKETING_AUTO=0.
    if os.environ.get('MARKETING_AUTO', '1') != '0':
        _scheduler.add_job(
            lambda app=app: _run_marketing(app),
            'cron', hour=9, minute=0, id='marketing-listmonk',
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
    # (default 15 min) e avalia via Haiku. Anti-spam: dedupe persistente em
    # VigiaVeredito + idade maxima + teto por ciclo.
    _scheduler.add_job(
        lambda app=app: _run_vigia_abandono(app),
        'cron', minute='*/5', id='vigia-abandono',
        max_instances=1, coalesce=True,
    )

    # Follow-up do bot — cliente sumiu apos mensagem nossa, o bot cutuca
    # (1x por conversa, janela 5-120min). Desligar: CHATBOT_FOLLOWUP=0.
    if os.environ.get('CHATBOT_FOLLOWUP', '1') != '0':
        _scheduler.add_job(
            lambda app=app: _run_followup_bot(app),
            'cron', minute='*/5', id='chatbot-followup',
            max_instances=1, coalesce=True,
        )

    # Libera reservas de estoque de pedidos online abandonados (Pix
    # expira em 30min, reserva em 35min). Cancela o pedido + devolve
    # saldo virtual ao catalogo. Idempotente.
    _scheduler.add_job(
        lambda app=app: _run_liberar_reservas_expiradas(app),
        'cron', minute='*/5', id='loja-reservas-expiradas',
        max_instances=1, coalesce=True,
    )

    _scheduler.start()
    logger.info('Auto-sync iniciado: Seru + VNDA 15min · resumo 04:00 · lembretes amanha 9h/12h/16h/19h · pedidos hoje 10-19h · zapi tarefas 07:00 · desperdicio slack 20:10/15/20/25 + whatsapp 20:30 · backup 04:00 · automacoes whatsapp 5min')


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


def _run_auto_pedidos(app):
    """Job: pedidos automáticos loja→indústria (motor venda+estoque,
    D+1..D+3). Best-effort: exceção fica no log do _com_lock, o scheduler
    nunca cai. Pedido de humano é preservado dentro do próprio service."""
    from app.services import auto_pedidos

    with app.app_context():
        _com_lock(LOCK_KEY_AUTO_PEDIDOS,
                  auto_pedidos.gerar_pedidos_automaticos,
                  'pedidos automaticos loja->industria')


def _run_ordens_semana(app):
    """Job do meio-dia (dono 17/08/2026): 1) PEDIDOS da semana loja→indústria
    (motor venda+estoque, amanhã..próximo domingo — "os pedidos da semana
    também devem ser lançados tudo no domingo meio dia"); 2) ORDENS de
    produção da semana (o firme recém-criado alimenta o grid). No domingo
    12:00 abre seg..dom; nos outros dias re-sincroniza/re-preenche. Pedido
    e ordem de humano nunca são tocados (regras nos services)."""
    from app.services import auto_pedidos

    with app.app_context():
        if os.environ.get('AUTO_PEDIDOS', '1') != '0':
            _com_lock(LOCK_KEY_AUTO_PEDIDOS,
                      auto_pedidos.gerar_pedidos_automaticos,
                      'pedidos da semana (meio-dia)')
        _com_lock(LOCK_KEY_AUTO_ENVIO,
                  auto_pedidos.enviar_ordens_da_semana,
                  'ordens de producao da semana')


# Sufixos do marker: 'b' = regra "producao so seg-sex" (mesma tarde) exigiu
# re-sincronizar a semana ja enviada; 'c' = pedidos da semana inteira no
# meio-dia (mesma tarde); 'd' = equilibrar carga virou padrao da automacao
# (mesma tarde, "o sistema deve equilibrar sozinho"); 'e' = nivelamento
# POR LOTES com antecedencia maxima (mesma noite, caso Brioche 160 num dia
# so — inclui re-nivelar a ordem DE HOJE, que o padeiro executa de
# madrugada e tinha absorvido o pico no 🔄 das 19:05); 'f' = nivelador
# refinado (alvo pelos dias uteis + antecedencia contra o dia de DEMANDA
# via ref_dia — sourdoughs fatiam e a parcela do fim de semana nao e
# adiantada de novo).
ORDENS_SEMANA_RETRO_MARKER = 'ordens_semana_retro_2026_08_17f'


def _run_ordens_semana_retro(app):
    """One-shot de RETROAÇÃO (dono 17/08/2026: "você vai ter que retroagir,
    a de ontem porque foi ontem o domingo"): a 1ª semana (pedidos + ordens)
    sai no primeiro boot após o deploy, sem esperar o próximo meio-dia. O
    marker em AppConfig garante que roda UMA vez — deploys futuros não
    re-executam; se a rodada falhar (exceção antes do marker), o próximo
    boot retenta."""
    from app.extensions import db
    from app.models import AppConfig
    from app.services import auto_pedidos

    def _fn():
        if AppConfig.get(ORDENS_SEMANA_RETRO_MARKER):
            return
        if os.environ.get('AUTO_PEDIDOS', '1') != '0':
            auto_pedidos.gerar_pedidos_automaticos()
        # A ordem DE HOJE tambem (retro 'e'): o 🔄 das 19:05 tinha aplicado
        # o nivelamento antigo (receita inteira num dia) na ordem que o
        # padeiro executa NESTA madrugada — re-nivela por lotes junto.
        auto_pedidos.atualizar_plano_automatico()
        auto_pedidos.enviar_ordens_da_semana()
        AppConfig.set(ORDENS_SEMANA_RETRO_MARKER,
                      datetime.now(timezone.utc).isoformat())
        db.session.commit()

    with app.app_context():
        _com_lock(LOCK_KEY_AUTO_ENVIO, _fn, 'ordens da semana (retro one-shot)')


def _run_atualiza_plano(app):
    """Job: re-sincroniza a ordem DE HOJE (criada pelo cron) com o grid —
    o 🔄 automático das 06:45/19:05. Ordem de humano nunca é tocada."""
    from app.services import auto_pedidos

    with app.app_context():
        _com_lock(LOCK_KEY_ATUALIZA_PLANO,
                  auto_pedidos.atualizar_plano_automatico,
                  'atualizacao automatica da ordem do dia')


def _run_tiny_pdv(app):
    """Job: importa as vendas do PDV do TINY (Cantina) e baixa o estoque.

    Janela ontem+hoje: o PDV pode faturar depois da virada e a API do Tiny
    as vezes atrasa a listagem. Idempotente por `TinyPedidoProcessado`, entao
    re-varrer o mesmo dia nao baixa duas vezes. Best-effort: NUNCA derruba o
    scheduler (o service ja engole excecao e devolve stats com `erro`).
    """
    from datetime import timedelta

    from app.services import tiny_pdv_sync
    from app.utils import hoje

    with app.app_context():
        if tiny_pdv_sync.loja_pdv_tiny() is None:
            return                       # nao configurado: nem tenta
        hj = hoje()
        _com_lock(LOCK_KEY_TINY_PDV,
                  lambda: tiny_pdv_sync.processar_periodo(
                      hj - timedelta(days=1), hj),
                  'import PDV Tiny')


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


def _run_digest_recebimentos(app):
    """Job: digest UNICO dos pedidos recebidos nas lojas, 12:00 BRT
    (14/08/2026, dono: "acumula ate as 12:00 e dispara uma unica mensagem
    ao inves de mandar picado"). Idempotente pelo sentinela em
    pedido.observacao; recebido apos as 12:00 entra no digest seguinte."""
    from app.services import pedidos_notificacao

    with app.app_context():
        _com_lock(LOCK_KEY_DIGEST_RECEBIMENTOS,
                  pedidos_notificacao.enviar_digest_recebimentos,
                  'digest pedidos recebidos')


def _run_automacoes_whatsapp(app):
    """Job: dispara as automacoes WhatsApp agendadas que estao no horario
    (checa a cada 5 min). Idempotente por dia via ultimo_disparo_em."""
    from app.services import whatsapp

    with app.app_context():
        _com_lock(7732, whatsapp.disparar_automacoes_devidas, 'automacoes whatsapp')


def _run_followup_bot(app):
    """Job: follow-up automatico do bot (12/06/2026, pedido do dono).
    Cliente que sumiu apos mensagem NOSSA em conversa pending recebe um
    cutucao gentil do proprio bot (1x por conversa, janela 5-120min).
    Guarda-corpos no servico (chatbot.followup_conversas_paradas).

    No MESMO ciclo roda a VASSOURA (02/07/2026): conversa pending cuja
    ultima msg e do CLIENTE ha 10+ min = o bot ficou devendo resposta
    (thread morta num deploy, crash pos-idempotencia) — reprocessa e
    responde. Kill-switch proprio: CHATBOT_VASSOURA=0."""
    from app.services import chatbot, chatwoot

    with app.app_context():
        if not chatwoot.bot_disponivel():
            return
        _com_lock(LOCK_KEY_FOLLOWUP, chatbot.followup_conversas_paradas,
                  'followup bot')
        _com_lock(LOCK_KEY_FOLLOWUP, chatbot.varrer_pendentes_sem_resposta,
                  'vassoura bot')


def _run_liberar_reservas_expiradas(app):
    """Job: libera reservas de estoque de pedidos online que abandonaram o
    checkout (Pix expira em 30min, reserva em 35min, cron varre a cada
    5min). Marca o pedido como cancelado e devolve o saldo virtual ao
    catalogo. Idempotente."""
    from app.services import loja_estoque_reserva

    with app.app_context():
        _com_lock(LOCK_KEY_RESERVA_EXPIRA,
                  loja_estoque_reserva.liberar_expirados,
                  'libera reservas expiradas')


def _run_vigia_chatwoot(app):
    """Job: vigia de infra do Chatwoot (15 em 15 min). Criado em
    12/06/2026 apos a equipe de atendimento descobrir o Chatwoot quebrado
    antes do sistema. Alerta o dono no WhatsApp na transicao
    saudavel→doente (anti-spam de 6h dentro do servico)."""
    from app.services import chatwoot

    with app.app_context():
        _com_lock(LOCK_KEY_VIGIA_CHATWOOT, chatwoot.vigiar_infra,
                  'vigia infra chatwoot')


def _run_site_vigia(app):
    """Job: vigia do SITE (05/07/2026) — canarios de frete (geocode em rua
    homonima virou R$95/bloqueio no incidente do dia), catalogo com produto
    vendavel e agenda com data/janela. Alerta o dono no WhatsApp na
    transicao saudavel→doente (anti-spam de 6h dentro do servico)."""
    from app.services import site_vigia

    with app.app_context():
        _com_lock(LOCK_KEY_SITE_VIGIA, site_vigia.vigiar, 'vigia site')


def _run_pdv_vigia(app):
    """Job: vigia do PDV (07/07/2026) — a baixa de venda parou em alguma
    loja? Alerta o dono no WhatsApp na transicao (anti-spam no servico)."""
    from app.services import pdv_vigia

    with app.app_context():
        _com_lock(LOCK_KEY_PDV_VIGIA, pdv_vigia.vigiar, 'vigia pdv')


def _run_uso_ia_vigia(app):
    """Job: vigia de custo de IA (11/07/2026) — gasto do dia em UsoIA
    estourou o teto? Alerta o dono no WhatsApp na transicao (anti-spam de
    6h no servico)."""
    from app.services import uso_ia_vigia

    with app.app_context():
        _com_lock(LOCK_KEY_USO_IA_VIGIA, uso_ia_vigia.vigiar,
                  'vigia uso ia')


def _run_google_reviews(app):
    """Job: sync de avaliacoes do Google (12/07/2026) — puxa reviews novas e
    alerta o dono no WhatsApp (prioriza nota baixa). No-op gracioso enquanto
    nao houver OAuth/aprovacao. Anti-flood do 1o sync no proprio servico."""
    from app.services import google_reviews

    with app.app_context():
        _com_lock(LOCK_KEY_GOOGLE_REVIEWS,
                  google_reviews.sincronizar_e_alertar, 'google reviews')


# O job `briefing-dono` (07:00 BRT) viveu de 16 a 17/07/2026 — removido a
# pedido do dono ("nao quero receber"). O envio manual continua pela rota
# /admin/briefing (owner), sem cron e sem lock.


def _run_marketing(app):
    """Job: marketing por e-mail (05/08/2026) — 09:00 BRT.

    Duas etapas na MESMA execucao e nesta ordem:
      1. `sincronizar()` empurra a base (site + Wi-Fi) pro Listmonk e traz de
         volta quem descadastrou;
      2. `campanha_aniversario()` monta e (se o dono ligou na tela) dispara a
         felicitacao do dia.

    O disparo nasce DESLIGADO: sem o gesto do dono em /admin/marketing a
    campanha e criada em RASCUNHO e nada sai. Kill-switch: MARKETING_AUTO=0.
    Best-effort — os dois servicos ja engolem excecao e devolvem `erro`.
    """
    from app.services import marketing

    def _fn():
        marketing.sincronizar()
        marketing.campanha_aniversario()

    with app.app_context():
        _com_lock(LOCK_KEY_MARKETING, _fn, 'marketing (Listmonk)')


def _run_alerta_baixas_presas(app):
    """Job: baixas presas (03/07/2026) — pedido 'separado' com entrega
    vencida (QR de saida nao lido = industria nao baixou) e retirada de
    sobra presa em transporte (loja baixou, industria nao creditada).
    Alerta o dono no WhatsApp; dedup de 6h dentro do servico."""
    from app.services import alertas_operacionais

    with app.app_context():
        _com_lock(LOCK_KEY_BAIXAS_PRESAS,
                  alertas_operacionais.rodar_e_alertar,
                  'alerta baixas presas')


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
        _gravar_marco_backup('backup_ultimo_run_em')
        if not resultado['ok']:
            logger.warning('backup diario falhou: %s', resultado.get('motivo'))
            return
        _gravar_marco_backup('backup_ultimo_ok_em')
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
        _gravar_marco_backup('backup_chatwoot_ultimo_run_em')
        if not resultado['ok']:
            logger.warning('backup chatwoot falhou: %s', resultado.get('motivo'))
        else:
            _gravar_marco_backup('backup_chatwoot_ultimo_ok_em')

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


def _gravar_marco_backup(chave):
    """Persiste o carimbo do backup em AppConfig (11/07/2026): o
    `_ult_run_backup` em memoria zera a cada deploy/restart — 'backup
    parado em silencio' ficava invisivel ate alguem precisar do dump.
    Best-effort: falha ao gravar o marco nunca derruba o job (o backup em
    si ja foi feito)."""
    from app.extensions import db
    from app.models import AppConfig
    from app.utils import agora as _agora
    try:
        AppConfig.set(chave, _agora().isoformat())
        db.session.commit()
    except Exception:  # noqa: BLE001
        logger.exception('falha ao gravar marco de backup %s', chave)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001 — rollback de conexao ja morta
            pass


def _parse_marco_backup(chave):
    """Le um marco de backup do AppConfig (datetime BRT-naive ou None)."""
    from datetime import datetime as _dt

    from app.models import AppConfig
    s = AppConfig.get(chave)
    if not s:
        return None
    try:
        return _dt.fromisoformat(s)
    except ValueError:
        return None


def status_backup():
    """Status do job backup pra UI. `ultimo_run`/`ultimo_ok` preferem o
    marco persistido em AppConfig (sobrevive a deploy); fallback pro valor
    em memoria de antes da persistencia. Defensivo: banco doente nao pode
    derrubar a pagina de diagnostico (que se abre exatamente quando as
    coisas quebram) — cai pro valor em memoria."""
    try:
        ultimo_run = (_parse_marco_backup('backup_ultimo_run_em')
                      or _ult_run_backup)
        ultimo_ok = _parse_marco_backup('backup_ultimo_ok_em')
    except Exception:  # noqa: BLE001
        logger.exception('status_backup: leitura do marco falhou')
        ultimo_run, ultimo_ok = _ult_run_backup, None
    return {
        'ativo': _scheduler is not None and _scheduler.running,
        'ultimo_run': ultimo_run,
        'ultimo_ok': ultimo_ok,
    }


def _run_slack_lembretes_pedidos_hoje(app):
    """Job: posta no #pedidos lembretes de pedidos de hoje nao entregues
    (10h, 11h, ..., 19h BRT). Advisory lock pra evitar duplicacao entre workers."""
    from app.services import slack_resumos

    with app.app_context():
        _com_lock(7727, slack_resumos.enviar_lembrete_pedidos_hoje_pendentes,
                  'slack lembrete pedidos hoje')


def _run_heartbeat_slack(app):
    """Heartbeat invertido: 1x por dia, posta no Slack 'sistema OK'.

    Pedido do dono (12/06/2026, apos auditoria detectar dependencia
    circular): se Z-API cair, NINGUEM avisa o dono (vigia de infra do
    Chatwoot usa Z-API; auditor usa Z-API; abandono usa Z-API). Slack e
    canal INDEPENDENTE — se o heartbeat sumir do canal por >12h, o dono
    sabe que a infra alertadora caiu, mesmo sem WhatsApp chegando.

    Configurar `SLACK_CHANNEL_HEARTBEAT` (id do canal de ops). Sem env,
    job nao registra (silencioso por design — heartbeat sem destino e
    so ruido)."""
    from app.services import slack

    with app.app_context():
        canal = (os.environ.get('SLACK_CHANNEL_HEARTBEAT')
                 or app.config.get('SLACK_CHANNEL_HEARTBEAT') or '').strip()
        if not canal:
            return
        from datetime import datetime
        from zoneinfo import ZoneInfo
        agora_brt = datetime.now(ZoneInfo('America/Sao_Paulo'))
        texto = (f':heartbeat: sistema OK · {agora_brt.strftime("%d/%m %H:%M")} BRT\n'
                 'se essa msg sumir do canal por mais de 24h, '
                 'a infra de alertas (Z-API/vigias) pode estar fora')
        aviso = _aviso_backup_atrasado()
        if aviso:
            texto += '\n' + aviso
        slack.post_message(canal, text=texto)


def _aviso_backup_atrasado(limite_horas=28):
    """Linha de aviso pro heartbeat quando o ultimo backup OK esta velho
    (dead-man's switch do backup, 11/07/2026): falha do job so logava
    WARNING e ninguem via — o backup podia parar por semanas em silencio.
    28h = job diario das 04:00 com folga pra atraso normal. Cobre o backup
    do sistema (gate `BACKUP_AUTO`) e o do Chatwoot (gates proprios:
    `BACKUP_CHATWOOT` + CHATWOOT_DATABASE_URL — espelham o agendamento).
    Job que RODA mas NUNCA registrou OK tambem avisa (run gravado + OK
    ausente = falhando desde sempre; sem isso o dead-man nasceria cego pro
    backup ja-quebrado). Quieto fora de Postgres (local) ou sem marco
    nenhum (primeiro 04:00 do deploy ainda nao rodou). Best-effort: erro
    aqui nunca derruba o heartbeat."""
    from flask import current_app

    from app.extensions import db
    from app.utils import agora as _agora
    try:
        if db.engine.dialect.name != 'postgresql':
            return None
        agora_dt = _agora()
        alvos = []
        if os.environ.get('BACKUP_AUTO', '1') != '0':
            alvos.append(('backup_ultimo_ok_em', 'backup_ultimo_run_em',
                          'backup do Postgres'))
        if (os.environ.get('BACKUP_CHATWOOT', '1') != '0'
                and (current_app.config.get('CHATWOOT_DATABASE_URL')
                     or '').strip()):
            alvos.append(('backup_chatwoot_ultimo_ok_em',
                          'backup_chatwoot_ultimo_run_em',
                          'backup do Chatwoot'))
        avisos = []
        for chave_ok, chave_run, rotulo in alvos:
            ultimo_ok = _parse_marco_backup(chave_ok)
            if ultimo_ok:
                horas = (agora_dt - ultimo_ok).total_seconds() / 3600
                if horas > limite_horas:
                    avisos.append(
                        f':warning: ultimo {rotulo} OK ha {int(horas)}h '
                        f'({ultimo_ok.strftime("%d/%m %H:%M")}) — o job '
                        'diario das 04:00 pode estar falhando; ver card '
                        'Backup em /admin/debug-schema')
                continue
            ultimo_run = _parse_marco_backup(chave_run)
            if ultimo_run:
                avisos.append(
                    f':warning: {rotulo} roda mas NUNCA registrou OK '
                    f'(ultimo run {ultimo_run.strftime("%d/%m %H:%M")}) — '
                    'falhando desde o inicio do marco; ver card Backup em '
                    '/admin/debug-schema')
        if avisos:
            return '\n'.join(avisos)
    except Exception:  # noqa: BLE001 — aviso nunca derruba o heartbeat
        logger.exception('aviso de backup atrasado falhou')
    return None

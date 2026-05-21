"""Auto-sync Seru → EstoqueLoja (cron 15 minutos).

APScheduler roda dentro de cada worker gunicorn. Pra evitar execucao
duplicada quando ha multiplos workers, usamos pg_try_advisory_lock no
PostgreSQL — so 1 worker pega o lock por vez, os outros pulam.

Ativado por default. Pra desligar em runtime: setar env var
SERU_AUTO_SYNC=0 antes do startup.
"""
import logging
import os

from sqlalchemy import text

from app.utils import hoje as hoje_brt

logger = logging.getLogger(__name__)

_scheduler = None
_ult_run = None
_ult_run_vnda = None
LOCK_KEY = 7723  # advisory lock pro Seru
LOCK_KEY_VNDA = 7724  # advisory lock pro VNDA


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
                # Processa SO o dia de HOJE (BRT). Vendas de ontem ou
                # anteriores nao sao tocadas — preferencia do usuario.
                hoje = hoje_brt()
                stats = seru_sync.processar_pedidos(hoje, hoje, user=None)
                _ult_run = _agora()
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
                hoje = hoje_brt()
                stats = vnda_sync.processar_pedidos(hoje, user=None)
                _ult_run_vnda = _agora()
                if stats.get('erro'):
                    logger.warning('vnda auto-sync erro: %s', stats['erro'])
                elif any(stats.get(k, 0) for k in (
                        'pedidos_novos', 'itens_baixados',
                        'pedidos_cancelados_estornados')):
                    logger.info('vnda auto-sync (com mudancas): %s', stats)
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

    # Digest WhatsApp de anomalias do dia — 23:00 BRT (apos fechamento)
    _scheduler.add_job(
        lambda app=app: _run_zapi_digest_anomalias(app),
        'cron', hour=23, minute=0, id='zapi-digest-anomalias',
        max_instances=1, coalesce=True,
    )

    _scheduler.start()
    logger.info('Auto-sync iniciado: Seru + VNDA 15min · resumo 04:00 · lembretes amanha 9h/12h/16h/19h · pedidos hoje 10-19h · zapi tarefas 07:00 · zapi anomalias 23:00')


def _run_slack_resumo_diario(app):
    """Job: posta o resumo de pedidos do dia no Slack (04:00 BRT).

    Usa pg_advisory_lock pra garantir 1x entre workers gunicorn.
    """
    from app.extensions import db
    from app.services import slack_resumos

    with app.app_context():
        uri = app.config.get('SQLALCHEMY_DATABASE_URI', '') or ''
        is_pg = 'postgresql' in uri
        try:
            if is_pg:
                with db.engine.connect() as c:
                    got = c.execute(text("SELECT pg_try_advisory_lock(7725)")).scalar()
                    if not got:
                        return  # outro worker pegou
            try:
                slack_resumos.enviar_resumo_pedidos_dia()
            finally:
                if is_pg:
                    with db.engine.connect() as c:
                        c.execute(text("SELECT pg_advisory_unlock(7725)"))
                        c.commit()
        except Exception:
            logger.exception('slack resumo diario falhou')


def _run_slack_lembretes_amanha(app):
    """Job: posta lembretes pra lojas sem pedido pra amanha (9/12/16/19h BRT)."""
    from app.extensions import db
    from app.services import slack_resumos

    with app.app_context():
        uri = app.config.get('SQLALCHEMY_DATABASE_URI', '') or ''
        is_pg = 'postgresql' in uri
        try:
            if is_pg:
                with db.engine.connect() as c:
                    got = c.execute(text("SELECT pg_try_advisory_lock(7726)")).scalar()
                    if not got:
                        return
            try:
                slack_resumos.enviar_lembretes_pedido_amanha()
            finally:
                if is_pg:
                    with db.engine.connect() as c:
                        c.execute(text("SELECT pg_advisory_unlock(7726)"))
                        c.commit()
        except Exception:
            logger.exception('slack lembrete pedido amanha falhou')


def _run_zapi_digest_tarefas(app):
    """Job: envia digest WhatsApp das tarefas do dia (07:00 BRT)."""
    from app.extensions import db
    from app.services import zapi_resumos

    with app.app_context():
        uri = app.config.get('SQLALCHEMY_DATABASE_URI', '') or ''
        is_pg = 'postgresql' in uri
        try:
            if is_pg:
                with db.engine.connect() as c:
                    got = c.execute(text("SELECT pg_try_advisory_lock(7728)")).scalar()
                    if not got:
                        return
            try:
                zapi_resumos.enviar_digest_tarefas()
            finally:
                if is_pg:
                    with db.engine.connect() as c:
                        c.execute(text("SELECT pg_advisory_unlock(7728)"))
                        c.commit()
        except Exception:
            logger.exception('zapi digest tarefas falhou')


def _run_zapi_digest_anomalias(app):
    """Job: envia digest WhatsApp de anomalias do dia (23:00 BRT)."""
    from app.extensions import db
    from app.services import anomalias

    with app.app_context():
        uri = app.config.get('SQLALCHEMY_DATABASE_URI', '') or ''
        is_pg = 'postgresql' in uri
        try:
            if is_pg:
                with db.engine.connect() as c:
                    got = c.execute(text("SELECT pg_try_advisory_lock(7730)")).scalar()
                    if not got:
                        return
            try:
                anomalias.enviar_digest_whatsapp()
            finally:
                if is_pg:
                    with db.engine.connect() as c:
                        c.execute(text("SELECT pg_advisory_unlock(7730)"))
                        c.commit()
        except Exception:
            logger.exception('zapi digest anomalias falhou')


def _run_slack_lembretes_pedidos_hoje(app):
    """Job: posta no #pedidos lembretes de pedidos de hoje nao entregues
    (10h, 11h, ..., 19h BRT). Advisory lock pra evitar duplicacao entre workers."""
    from app.extensions import db
    from app.services import slack_resumos

    with app.app_context():
        uri = app.config.get('SQLALCHEMY_DATABASE_URI', '') or ''
        is_pg = 'postgresql' in uri
        try:
            if is_pg:
                with db.engine.connect() as c:
                    got = c.execute(text("SELECT pg_try_advisory_lock(7727)")).scalar()
                    if not got:
                        return
            try:
                slack_resumos.enviar_lembrete_pedidos_hoje_pendentes()
            finally:
                if is_pg:
                    with db.engine.connect() as c:
                        c.execute(text("SELECT pg_advisory_unlock(7727)"))
                        c.commit()
        except Exception:
            logger.exception('slack lembrete pedidos hoje falhou')

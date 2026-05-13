"""Auto-sync Seru → EstoqueLoja (cron 15 minutos).

APScheduler roda dentro de cada worker gunicorn. Pra evitar execucao
duplicada quando ha multiplos workers, usamos pg_try_advisory_lock no
PostgreSQL — so 1 worker pega o lock por vez, os outros pulam.

Ativado por default. Pra desligar em runtime: setar env var
SERU_AUTO_SYNC=0 antes do startup.
"""
import logging
import os
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)

_scheduler = None
_ult_run = None
_ult_run_vnda = None
LOCK_KEY = 7723  # advisory lock pro Seru
LOCK_KEY_VNDA = 7724  # advisory lock pro VNDA
BRT = timezone(timedelta(hours=-3))


def hoje_brt():
    """Data 'hoje' em horario de Brasilia (Railway roda em UTC)."""
    return datetime.now(BRT).date()


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
    from datetime import datetime as _dt

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
                _ult_run = _dt.utcnow()
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
    from datetime import datetime as _dt

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
                _ult_run_vnda = _dt.utcnow()
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
    _scheduler.start()
    logger.info('Auto-sync iniciado: Seru + VNDA a cada 15min')

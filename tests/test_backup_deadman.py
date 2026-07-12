"""Dead-man do backup diário (resgatado da sessão de 12/07/2026): marcos
persistidos em AppConfig + aviso no heartbeat quando o último backup OK
está velho ou o job roda mas nunca deu OK. A sessão original não deixou
testes — estes travam os ramos principais.
"""
from datetime import timedelta

from app.extensions import db
from app.models import AppConfig
from app.services import seru_cron as sc
from app.utils import agora


def _pg(monkeypatch):
    """O aviso é quieto fora de Postgres — simula o dialect em SQLite."""
    monkeypatch.setattr(db.engine.dialect, 'name', 'postgresql')


def test_marco_roundtrip_e_status_backup(app):
    with app.app_context():
        sc._gravar_marco_backup('backup_ultimo_run_em')
        sc._gravar_marco_backup('backup_ultimo_ok_em')
        st = sc.status_backup()
        assert st['ultimo_run'] is not None
        assert st['ultimo_ok'] is not None
        # marco ilegível não explode a página de diagnóstico
        AppConfig.set('backup_ultimo_ok_em', 'não-é-data')
        db.session.commit()
        assert sc.status_backup()['ultimo_ok'] is None


def test_aviso_quieto_com_ok_recente_e_sem_marco(app, monkeypatch):
    with app.app_context():
        _pg(monkeypatch)
        monkeypatch.delenv('BACKUP_CHATWOOT', raising=False)
        app.config['CHATWOOT_DATABASE_URL'] = ''
        assert sc._aviso_backup_atrasado() is None      # sem marco algum
        AppConfig.set('backup_ultimo_ok_em', agora().isoformat())
        db.session.commit()
        assert sc._aviso_backup_atrasado() is None      # OK recente


def test_aviso_quando_ok_esta_velho(app, monkeypatch):
    with app.app_context():
        _pg(monkeypatch)
        app.config['CHATWOOT_DATABASE_URL'] = ''
        AppConfig.set('backup_ultimo_ok_em',
                      (agora() - timedelta(hours=40)).isoformat())
        db.session.commit()
        aviso = sc._aviso_backup_atrasado()
        assert aviso is not None
        assert 'backup do Postgres OK ha' in aviso


def test_aviso_quando_roda_mas_nunca_deu_ok(app, monkeypatch):
    with app.app_context():
        _pg(monkeypatch)
        app.config['CHATWOOT_DATABASE_URL'] = ''
        AppConfig.set('backup_ultimo_run_em', agora().isoformat())
        db.session.commit()
        aviso = sc._aviso_backup_atrasado()
        assert aviso is not None
        assert 'NUNCA registrou OK' in aviso


def test_kill_switch_backup_auto_silencia(app, monkeypatch):
    with app.app_context():
        _pg(monkeypatch)
        monkeypatch.setenv('BACKUP_AUTO', '0')
        app.config['CHATWOOT_DATABASE_URL'] = ''
        AppConfig.set('backup_ultimo_ok_em',
                      (agora() - timedelta(hours=99)).isoformat())
        db.session.commit()
        assert sc._aviso_backup_atrasado() is None

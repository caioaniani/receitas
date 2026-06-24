"""Testes do servico de backup Postgres → Dropbox."""
import subprocess
from unittest.mock import patch


def test_backup_pula_sqlite(app):
    """Em ambiente de teste (SQLite), backup retorna ok=False sem rodar pg_dump."""
    from app.services import backup

    with app.app_context():
        r = backup.executar_backup(forcar=True)

    assert r['ok'] is False
    assert 'nao eh Postgres' in r['motivo']
    assert r['tamanho'] == 0


def test_backup_falha_sem_dropbox(app):
    """Postgres + Dropbox nao configurado → retorna motivo claro."""
    from app.services import backup

    with app.app_context():
        app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://u:p@h/db'
        with patch('app.services.dropbox_storage.disponivel', return_value=False):
            r = backup.executar_backup(forcar=True)

    assert r['ok'] is False
    assert 'Dropbox' in r['motivo']


def test_backup_executa_pg_dump_e_upload(app):
    """Caminho feliz: pg_dump retorna bytes, upload sobe pro Dropbox."""
    from app.services import backup

    dump_bytes_fake = b'PGDMP' + b'x' * 1000  # bytes arbitrarios
    chamadas_subprocess = []

    def _fake_subprocess(cmd, env=None, **kwargs):
        chamadas_subprocess.append({'cmd': cmd, 'env': env})
        return subprocess.CompletedProcess(args=cmd, returncode=0,
                                           stdout=dump_bytes_fake, stderr=b'')

    chamadas_upload = []

    def _fake_upload(file_bytes, dropbox_path):
        chamadas_upload.append({'bytes': file_bytes, 'path': dropbox_path})
        return {'storage_path': dropbox_path, 'tamanho': len(file_bytes)}

    with app.app_context():
        app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://u%40x:senha%21@h:5432/padaria'
        with patch('app.services.dropbox_storage.disponivel', return_value=True), \
             patch('app.services.backup.subprocess.run', side_effect=_fake_subprocess), \
             patch('app.services.dropbox_storage.upload_arquivo', side_effect=_fake_upload):
            r = backup.executar_backup(forcar=True)

    assert r['ok'] is True, f'esperava ok, veio {r}'
    assert r['arquivo'].startswith('/backups-postgres/padaria_')
    assert r['arquivo'].endswith('.dump.gz')

    # pg_dump recebeu host/port/user corretos
    assert len(chamadas_subprocess) == 1
    cmd = chamadas_subprocess[0]['cmd']
    assert cmd[0] == 'pg_dump'
    assert '-h' in cmd and 'h' in cmd
    assert '-U' in cmd
    assert 'u@x' in cmd  # url-decoded
    assert '-d' in cmd
    assert 'padaria' in cmd
    assert '--format=custom' in cmd
    # Senha em env, NUNCA em argv
    assert chamadas_subprocess[0]['env']['PGPASSWORD'] == 'senha!'
    assert 'senha!' not in cmd

    # upload sobe bytes gzippados (header gzip = 1f 8b)
    assert len(chamadas_upload) == 1
    enviado = chamadas_upload[0]['bytes']
    assert enviado[:2] == b'\x1f\x8b', 'esperava arquivo gzip'


def test_backup_pg_dump_falha(app):
    """pg_dump retorna exit != 0 → retorna ok=False com stderr."""
    from app.services import backup

    def _fake_subprocess(cmd, env=None, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1, cmd=cmd, stderr=b'connection refused'
        )

    with app.app_context():
        app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://u:p@h/db'
        with patch('app.services.dropbox_storage.disponivel', return_value=True), \
             patch('app.services.backup.subprocess.run', side_effect=_fake_subprocess):
            r = backup.executar_backup(forcar=True)

    assert r['ok'] is False
    assert 'pg_dump falhou' in r['motivo']
    assert 'connection refused' in r['motivo']


def test_backup_sem_pg_dump_no_path(app):
    """Container sem postgresql-client → erro claro com pointer pro nixpacks."""
    from app.services import backup

    def _fake_subprocess(cmd, env=None, **kwargs):
        raise FileNotFoundError('pg_dump')

    with app.app_context():
        app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://u:p@h/db'
        with patch('app.services.dropbox_storage.disponivel', return_value=True), \
             patch('app.services.backup.subprocess.run', side_effect=_fake_subprocess):
            r = backup.executar_backup(forcar=True)

    assert r['ok'] is False
    assert 'nixpacks' in r['motivo']

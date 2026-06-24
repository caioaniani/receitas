"""Backup do banco do Chatwoot reusando `executar_backup` com db_url/prefixo."""
import subprocess
from unittest.mock import patch


def test_backup_aceita_db_url_prefixo_e_pasta(app):
    """`executar_backup(db_url=..., prefixo='chatwoot', pasta=...)` dumpa o
    banco alvo e sobe com o nome/pasta certos, sem tocar no banco do sistema."""
    from app.services import backup

    dump_bytes = b'PGDMP' + b'x' * 200
    chamadas = []

    def _fake_run(cmd, env=None, **kwargs):
        chamadas.append({'cmd': cmd, 'env': env})
        return subprocess.CompletedProcess(args=cmd, returncode=0,
                                           stdout=dump_bytes, stderr=b'')

    uploads = []

    def _fake_upload(file_bytes, dropbox_path):
        uploads.append(dropbox_path)
        return {'storage_path': dropbox_path, 'tamanho': len(file_bytes)}

    with app.app_context():
        with patch('app.services.dropbox_storage.disponivel', return_value=True), \
             patch('app.services.backup.subprocess.run', side_effect=_fake_run), \
             patch('app.services.dropbox_storage.upload_arquivo', side_effect=_fake_upload):
            r = backup.executar_backup(
                forcar=True,
                db_url='postgresql://cw:senha@host:5432/chatwoot_prod',
                prefixo='chatwoot', pasta='/backups-chatwoot')

    assert r['ok'] is True, f'esperava ok, veio {r}'
    assert r['arquivo'].startswith('/backups-chatwoot/chatwoot_')
    assert r['arquivo'].endswith('.dump.gz')
    # dumpou o banco do CHATWOOT (não o do sistema)
    cmd = chamadas[0]['cmd']
    assert 'chatwoot_prod' in cmd
    assert chamadas[0]['env']['PGPASSWORD'] == 'senha'
    assert 'senha' not in cmd  # senha nunca em argv
    assert uploads[0].startswith('/backups-chatwoot/chatwoot_')


def test_backup_default_inalterado(app):
    """Sem db_url, o comportamento antigo (padaria/backups-postgres) persiste."""
    from app.services import backup

    def _fake_run(cmd, env=None, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=0,
                                           stdout=b'PGDMP' + b'y' * 50, stderr=b'')

    with app.app_context():
        app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql://u:p@h:5432/padaria'
        with patch('app.services.dropbox_storage.disponivel', return_value=True), \
             patch('app.services.backup.subprocess.run', side_effect=_fake_run), \
             patch('app.services.dropbox_storage.upload_arquivo',
                   side_effect=lambda b, p: {'storage_path': p, 'tamanho': len(b)}):
            r = backup.executar_backup(forcar=True)

    assert r['ok'] is True
    assert r['arquivo'].startswith('/backups-postgres/padaria_')

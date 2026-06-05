"""Backup automatico do Postgres pro Dropbox.

Roda como job APScheduler diario (04:00 BRT) — ver `seru_cron.py`.
Tambem pode ser disparado manual via rota `/admin/backup/run`.

Implementacao:
1. Parsea DATABASE_URL.
2. Roda `pg_dump --format=custom` (formato binario comprimido com zlib).
3. Faz upload pro Dropbox via `dropbox_storage.upload_arquivo`.

Senha do banco vai via PGPASSWORD (env var), nao via argv — evita
exposicao na lista de processos.

Em SQLite local (dev), pula com aviso: backup nao faz sentido pra dev.
"""
import gzip
import logging
import os
import subprocess
from urllib.parse import unquote, urlparse

from flask import current_app

from app.services import dropbox_storage
from app.utils import agora as _agora_brt

logger = logging.getLogger(__name__)


def _pasta_destino():
    return (current_app.config.get('DROPBOX_BACKUP_PASTA')
            or os.environ.get('DROPBOX_BACKUP_PASTA')
            or '/backups-postgres').rstrip('/')


def _parse_database_url(url):
    """Quebra postgresql://user:pass@host:port/db em componentes."""
    parsed = urlparse(url)
    if parsed.scheme not in ('postgresql', 'postgres', 'postgresql+psycopg2'):
        return None
    return {
        'host': parsed.hostname or 'localhost',
        'port': str(parsed.port or 5432),
        'user': unquote(parsed.username or ''),
        'password': unquote(parsed.password or ''),
        'dbname': (parsed.path or '/').lstrip('/'),
    }


def executar_backup(forcar=False, db_url=None, prefixo='padaria', pasta=None):
    """Executa 1 ciclo de backup. Retorna dict com {ok, tamanho, arquivo}.

    `forcar=True` ignora checagem de SQLite (usado em rota manual de teste).
    `db_url` aponta pra um banco alternativo (ex: o Postgres do Chatwoot);
    None = banco do proprio sistema (SQLALCHEMY_DATABASE_URI).
    `prefixo` nomeia o arquivo (`<prefixo>_<timestamp>.dump.gz`).
    `pasta` sobrescreve a pasta destino no Dropbox (None = padrao).
    """
    uri = (db_url or current_app.config.get('SQLALCHEMY_DATABASE_URI', '') or '')
    if not uri.startswith(('postgresql', 'postgres')):
        msg = f'Backup ignorado: banco nao eh Postgres (uri={uri[:30]}...)'
        logger.info('[backup] %s', msg)
        return {'ok': False, 'motivo': msg, 'tamanho': 0, 'arquivo': ''}

    if not dropbox_storage.disponivel():
        msg = 'Dropbox nao configurado (faltam DROPBOX_APP_KEY/SECRET/REFRESH_TOKEN)'
        logger.warning('[backup] %s', msg)
        return {'ok': False, 'motivo': msg, 'tamanho': 0, 'arquivo': ''}

    conn = _parse_database_url(uri)
    if not conn:
        msg = 'Nao consegui parsear DATABASE_URL'
        logger.error('[backup] %s', msg)
        return {'ok': False, 'motivo': msg, 'tamanho': 0, 'arquivo': ''}

    # pg_dump custom format (-Fc) ja eh comprimido (zlib).
    # Gzip por cima da pouco ganho extra mas custa CPU; mantemos pra
    # compatibilidade com convencao .sql.gz dos backups antigos do projeto.
    cmd = [
        'pg_dump',
        '-h', conn['host'],
        '-p', conn['port'],
        '-U', conn['user'],
        '-d', conn['dbname'],
        '--format=custom',
        '--no-owner',
        '--no-acl',
    ]
    env = os.environ.copy()
    env['PGPASSWORD'] = conn['password']

    try:
        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            check=True,
            timeout=600,  # 10 min — bancos pequenos terminam em segundos
        )
    except FileNotFoundError:
        msg = 'pg_dump nao encontrado no PATH (verifique nixpacks.toml)'
        logger.exception('[backup] %s', msg)
        return {'ok': False, 'motivo': msg, 'tamanho': 0, 'arquivo': ''}
    except subprocess.TimeoutExpired:
        msg = 'pg_dump excedeu 10min'
        logger.exception('[backup] %s', msg)
        return {'ok': False, 'motivo': msg, 'tamanho': 0, 'arquivo': ''}
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b'').decode('utf-8', errors='replace')[:500]
        msg = f'pg_dump falhou (exit={e.returncode}): {stderr}'
        logger.error('[backup] %s', msg)
        return {'ok': False, 'motivo': msg, 'tamanho': 0, 'arquivo': ''}

    dump_bytes = proc.stdout
    if not dump_bytes:
        msg = 'pg_dump produziu 0 bytes'
        logger.error('[backup] %s', msg)
        return {'ok': False, 'motivo': msg, 'tamanho': 0, 'arquivo': ''}

    gz_bytes = gzip.compress(dump_bytes, compresslevel=6)
    timestamp = _agora_brt().strftime('%Y-%m-%d_%H%M')
    destino = (pasta or _pasta_destino()).rstrip('/')
    dropbox_path = f'{destino}/{prefixo}_{timestamp}.dump.gz'

    try:
        resultado = dropbox_storage.upload_arquivo(gz_bytes, dropbox_path)
    except RuntimeError as e:
        msg = f'Upload Dropbox falhou: {e}'
        logger.exception('[backup] %s', msg)
        return {'ok': False, 'motivo': msg, 'tamanho': len(gz_bytes), 'arquivo': dropbox_path}

    tamanho_mb = resultado['tamanho'] / 1024 / 1024
    logger.info('[backup] OK | %.2f MB | %s', tamanho_mb, resultado['storage_path'])
    return {
        'ok': True,
        'tamanho': resultado['tamanho'],
        'arquivo': resultado['storage_path'],
    }

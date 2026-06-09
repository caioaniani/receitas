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


# ── Drill de restore ───────────────────────────────────────────────────
#
# "Backup que nunca foi restaurado eh esperanca, nao backup." O drill prova
# que o dump do Dropbox eh restauravel DE VERDADE: baixa o mais recente,
# valida o TOC com pg_restore --list e (modo full) restaura num banco
# temporario, conta linhas de tabelas-chave e dropa o banco.
#
# Roda em thread (restore de ~100MB estoura o timeout HTTP do gunicorn).
# Status persistido em ARQUIVO (/tmp) e nao em memoria: gunicorn tem 2
# workers, e status em dict fazia a rota de consulta cair 50% das vezes no
# worker errado ("rodando: false" sem resultado — visto em prod 2026-06-09).
# Arquivo compartilha entre workers e sobrevive a reciclagem do processo.

_DRILL_STATUS_PATH = '/tmp/padaria_drill_status.json'
# Drill rodando ha mais que isso = worker morreu no meio (OOM/restart);
# considera abandonado e permite iniciar outro.
_DRILL_TIMEOUT_MIN = 30

_DRILL_DB = 'drill_restore_tmp'
# Tabelas-chave: se restauraram com linhas, o dump cobre o nucleo do negocio.
_DRILL_TABELAS_CHAVE = ('usuario', 'pedido_loja', 'receita', 'estoque_loja')


def _drill_salvar(status):
    import json as _json
    try:
        with open(_DRILL_STATUS_PATH, 'w', encoding='utf-8') as f:
            _json.dump(status, f, ensure_ascii=False, default=str)
    except OSError:
        logger.exception('[drill] falha gravando status em %s', _DRILL_STATUS_PATH)


def drill_status():
    import json as _json
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    try:
        with open(_DRILL_STATUS_PATH, encoding='utf-8') as f:
            st = _json.load(f)
    except (OSError, ValueError):
        return {'rodando': False, 'iniciado_em': None, 'resultado': None}
    # Detecta drill abandonado (worker morreu no meio — ex: OOM no restore):
    # sem isso, 'rodando: true' orfao bloquearia novos drills pra sempre.
    if st.get('rodando') and st.get('iniciado_em'):
        try:
            inicio = _dt.fromisoformat(st['iniciado_em'])
            if _agora_brt() - inicio > _td(minutes=_DRILL_TIMEOUT_MIN):
                st['rodando'] = False
                st['resultado'] = st.get('resultado') or {
                    'ok': False,
                    'motivo': (f'drill abandonado (>{_DRILL_TIMEOUT_MIN}min sem '
                               'terminar — worker provavelmente reiniciou no meio). '
                               'Rode de novo com ?iniciar=full.')}
        except ValueError:
            pass
    return st


def iniciar_drill(full=False):
    """Dispara o drill em background. Retorna o status (ou erro se ja rodando)."""
    import threading

    from flask import current_app as _app
    atual = drill_status()
    if atual.get('rodando'):
        return {'ok': False, 'motivo': 'drill ja em andamento', **atual}
    app_obj = _app._get_current_object()
    inicio_iso = _agora_brt().isoformat()
    _drill_salvar({'rodando': True, 'iniciado_em': inicio_iso, 'resultado': None})

    def _run():
        resultado = None
        try:
            with app_obj.app_context():
                resultado = _executar_drill(full=full)
        except Exception as exc:  # noqa: BLE001
            logger.exception('[drill] falha inesperada')
            resultado = {'ok': False, 'motivo': f'{type(exc).__name__}: {exc}'}
        finally:
            _drill_salvar({'rodando': False, 'iniciado_em': inicio_iso,
                           'terminado_em': _agora_brt().isoformat(),
                           'resultado': resultado})

    threading.Thread(target=_run, daemon=True).start()
    return {'ok': True, 'iniciado': True, 'full': full}


def _executar_drill(full=False):
    """Corpo do drill. Retorna relatorio dict (sempre, nunca levanta)."""
    import tempfile

    from app.services import dropbox_storage

    rel = {'ok': False, 'full': full, 'etapas': []}

    # 1. Acha o dump mais recente
    arquivos = dropbox_storage.listar_pasta(_pasta_destino())
    dumps = sorted((a for a in arquivos if a['nome'].endswith('.dump.gz')),
                   key=lambda a: a['nome'], reverse=True)
    if not dumps:
        rel['motivo'] = f'nenhum .dump.gz em {_pasta_destino()}'
        return rel
    alvo = dumps[0]
    rel['arquivo'] = alvo['nome']
    rel['tamanho_mb'] = round(alvo['tamanho'] / 1024 / 1024, 2)
    rel['etapas'].append('listado')

    # 2. Baixa e descomprime
    gz = dropbox_storage.baixar(alvo['path'])
    if not gz:
        rel['motivo'] = 'download do Dropbox falhou'
        return rel
    rel['etapas'].append('baixado')
    try:
        dump_bytes = gzip.decompress(gz)
    except OSError as exc:
        rel['motivo'] = f'gunzip falhou (dump corrompido?): {exc}'
        return rel
    rel['etapas'].append('descomprimido')

    with tempfile.NamedTemporaryFile(suffix='.dump', delete=True) as tmp:
        tmp.write(dump_bytes)
        tmp.flush()

        # 3. Valida o TOC: pg_restore --list le o indice do dump custom.
        # Dump truncado/corrompido falha aqui, mesmo sem banco de destino.
        try:
            proc = subprocess.run(['pg_restore', '--list', tmp.name],
                                  capture_output=True, timeout=120)
        except FileNotFoundError:
            rel['motivo'] = 'pg_restore nao encontrado no PATH'
            return rel
        except subprocess.TimeoutExpired:
            rel['motivo'] = 'pg_restore --list excedeu 2min'
            return rel
        if proc.returncode != 0:
            rel['motivo'] = ('pg_restore --list falhou: '
                             + (proc.stderr or b'').decode('utf-8', 'replace')[:300])
            return rel
        toc = (proc.stdout or b'').decode('utf-8', 'replace')
        rel['toc_tabelas'] = sum(1 for ln in toc.splitlines()
                                 if ' TABLE ' in ln and ' TABLE DATA ' not in ln)
        rel['toc_table_data'] = sum(1 for ln in toc.splitlines()
                                    if ' TABLE DATA ' in ln)
        rel['etapas'].append('toc_validado')
        if rel['toc_tabelas'] < 10:
            rel['motivo'] = (f'TOC suspeito: so {rel["toc_tabelas"]} tabelas '
                             '(esperado ~90). Dump pode estar incompleto.')
            return rel

        if not full:
            rel['ok'] = True
            rel['motivo'] = ('Dump integro (TOC validado). Pra prova completa '
                             'de restore, rode com ?iniciar=full.')
            return rel

        # 4. FULL: restaura num banco temporario e conta linhas
        return _drill_full(tmp.name, rel)


def _drill_full(dump_path, rel):
    from sqlalchemy import create_engine, text

    uri = current_app.config.get('SQLALCHEMY_DATABASE_URI', '')
    conn_info = _parse_database_url(uri)
    if not conn_info:
        rel['motivo'] = 'banco do app nao eh Postgres — full drill indisponivel'
        return rel

    admin_engine = create_engine(uri, isolation_level='AUTOCOMMIT',
                                 pool_pre_ping=True)
    try:
        with admin_engine.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS {_DRILL_DB}'))
            c.execute(text(f'CREATE DATABASE {_DRILL_DB}'))
    except Exception as exc:  # noqa: BLE001
        # Sem permissao de CREATE DATABASE (alguns planos restringem):
        # o TOC ja foi validado, entao reporta degradado em vez de falhar.
        rel['ok'] = True
        rel['motivo'] = (f'TOC OK, mas CREATE DATABASE falhou ({exc}) — '
                         'restore completo indisponivel neste servidor. '
                         'Valide manualmente num Postgres local com: '
                         'pg_restore -d test --no-owner --no-acl <arquivo>')
        admin_engine.dispose()
        return rel
    rel['etapas'].append('db_temp_criado')

    try:
        cmd = ['pg_restore',
               '-h', conn_info['host'], '-p', conn_info['port'],
               '-U', conn_info['user'], '-d', _DRILL_DB,
               '--no-owner', '--no-acl', dump_path]
        env = os.environ.copy()
        env['PGPASSWORD'] = conn_info['password']
        proc = subprocess.run(cmd, env=env, capture_output=True, timeout=900)
        # pg_restore devolve 1 em warnings benignos (ex: extensao ja existe);
        # o veredito real vem das CONTAGENS abaixo, nao do exit code.
        rel['restore_exit'] = proc.returncode
        if proc.returncode not in (0, 1):
            rel['motivo'] = ('pg_restore falhou: '
                             + (proc.stderr or b'').decode('utf-8', 'replace')[:300])
            return rel
        rel['etapas'].append('restaurado')

        drill_uri = uri.rsplit('/', 1)[0] + f'/{_DRILL_DB}'
        drill_engine = create_engine(drill_uri, pool_pre_ping=True)
        contagens = {}
        with drill_engine.connect() as c:
            for t in _DRILL_TABELAS_CHAVE:
                try:
                    contagens[t] = c.execute(
                        text(f'SELECT count(*) FROM {t}')).scalar()
                except Exception:  # noqa: BLE001
                    contagens[t] = 'AUSENTE'
        drill_engine.dispose()
        rel['contagens'] = contagens
        rel['etapas'].append('contado')

        vazio = [t for t, n in contagens.items()
                 if n == 'AUSENTE' or (isinstance(n, int) and n == 0
                                       and t != 'estoque_loja')]
        if vazio:
            rel['motivo'] = f'Restore subiu mas tabelas-chave vazias/ausentes: {vazio}'
            return rel

        rel['ok'] = True
        rel['motivo'] = 'Restore COMPLETO validado: dump restauravel de ponta a ponta.'
        return rel
    except subprocess.TimeoutExpired:
        rel['motivo'] = 'pg_restore excedeu 15min'
        return rel
    finally:
        try:
            with admin_engine.connect() as c:
                c.execute(text(f'DROP DATABASE IF EXISTS {_DRILL_DB} WITH (FORCE)'))
            rel['etapas'].append('db_temp_dropado')
        except Exception:  # noqa: BLE001
            logger.exception('[drill] DROP DATABASE %s falhou — dropar manualmente',
                             _DRILL_DB)
            rel['aviso'] = f'banco temporario {_DRILL_DB} pode ter ficado — dropar manualmente'
        admin_engine.dispose()

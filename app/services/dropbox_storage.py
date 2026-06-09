"""Upload de fotos de entrega no Dropbox.

Usa a Dropbox HTTP API direto (sem SDK) pra evitar dependencia extra.

Dois modos de autenticacao:
1. **Refresh token (recomendado)**: DROPBOX_APP_KEY + DROPBOX_APP_SECRET +
   DROPBOX_REFRESH_TOKEN. Refresh token nao expira; o servico troca por
   access tokens curtos automaticamente e cacheia em memoria. Setup
   one-shot via /entregas/dropbox/setup.
2. **Access token direto (curto)**: DROPBOX_ACCESS_TOKEN gerado no
   painel — expira em 4h. So serve pra teste rapido.

Pasta base: DROPBOX_PASTA_BASE (default /Apps/Receitas-Entregas).
Em apps "App folder", o Dropbox prefixa /Apps/<nome-do-app>/ automaticamente.
"""
import json
import logging
import threading
import time
import uuid

import requests
from flask import current_app

from app.utils import agora as _agora_brt

logger = logging.getLogger(__name__)

# Cache do access token curto (gerado a partir do refresh)
_token_cache = {'value': None, 'expira_em': 0}
_token_lock = threading.Lock()


def _pasta_base():
    return (current_app.config.get('DROPBOX_PASTA_BASE') or '/Apps/Receitas-Entregas').rstrip('/')


def _refresh_config():
    cfg = current_app.config
    return (
        (cfg.get('DROPBOX_APP_KEY') or '').strip(),
        (cfg.get('DROPBOX_APP_SECRET') or '').strip(),
        (cfg.get('DROPBOX_REFRESH_TOKEN') or '').strip(),
    )


def _legacy_token():
    return (current_app.config.get('DROPBOX_ACCESS_TOKEN') or '').strip()


def _token():
    """Retorna access token valido. Usa cache, refaz via refresh se preciso.

    Prioridade: refresh flow > token legado. Assim que o admin configura
    o refresh, o token legado de 4h passa a ser ignorado e pode ser apagado.
    """
    app_key, app_secret, refresh = _refresh_config()
    if app_key and app_secret and refresh:
        with _token_lock:
            agora = time.time()
            if _token_cache['value'] and _token_cache['expira_em'] - 60 > agora:
                return _token_cache['value']

            r = requests.post(
                'https://api.dropbox.com/oauth2/token',
                data={
                    'grant_type': 'refresh_token',
                    'refresh_token': refresh,
                },
                auth=(app_key, app_secret),
                timeout=10,
            )
            if r.status_code != 200:
                logger.warning('Dropbox refresh falhou: %s %s', r.status_code, r.text[:200])
                return ''
            body = r.json()
            access = body.get('access_token') or ''
            ttl = int(body.get('expires_in') or 14400)  # default 4h
            _token_cache['value'] = access
            _token_cache['expira_em'] = agora + ttl
            return access

    # Fallback: token legado (4h, so pra teste)
    return _legacy_token()


def disponivel():
    """True se token direto OU refresh flow estao configurados."""
    if _legacy_token():
        return True
    app_key, app_secret, refresh = _refresh_config()
    return bool(app_key and app_secret and refresh)


def _invalidar_cache():
    with _token_lock:
        _token_cache['value'] = None
        _token_cache['expira_em'] = 0


def upload_publico(file_bytes, dropbox_path, *, mode='add', autorename=True):
    """Sobe bytes pro Dropbox e cria shared link publico (URL retornada).

    Use pra fotos/imagens que o app precisa servir via `<img src=...>`.
    Diferente de `upload_arquivo` (que nao cria link).

    - `mode='add'` + `autorename=True`: se path ja existe, Dropbox sufixa
      `(1)`, `(2)`. Default seguro.
    - `mode='overwrite'`: substitui arquivo existente (pra cardapio onde
      a foto representa o item atual).

    Retorna {'url', 'storage_path', 'tamanho'}.
    Levanta RuntimeError se nao configurado ou API falhar.
    """
    if not file_bytes:
        raise RuntimeError('Arquivo vazio')
    if not dropbox_path or not dropbox_path.startswith('/'):
        raise RuntimeError('dropbox_path deve comecar com /')

    api_args = {
        'path': dropbox_path,
        'mode': mode,
        'autorename': autorename,
        'mute': True,
    }

    def _do_upload():
        token = _token()
        if not token:
            raise RuntimeError('Dropbox nao configurado')
        return requests.post(
            'https://content.dropboxapi.com/2/files/upload',
            headers={
                'Authorization': f'Bearer {token}',
                'Dropbox-API-Arg': json.dumps(api_args),
                'Content-Type': 'application/octet-stream',
            },
            data=file_bytes,
            timeout=30,
        )

    r = _do_upload()
    if r.status_code == 401:
        _invalidar_cache()
        r = _do_upload()
    if r.status_code != 200:
        logger.warning('Dropbox upload falhou: %s %s', r.status_code, r.text[:200])
        raise RuntimeError(f'Upload Dropbox falhou: {r.status_code}')

    meta = r.json()
    storage_path = meta.get('path_lower') or dropbox_path
    url = _criar_shared_link(_token(), storage_path)

    return {
        'url': url,
        'storage_path': storage_path,
        'tamanho': meta.get('size') or len(file_bytes),
    }


def upload_foto(file_bytes, atribuicao_id, ext='jpg'):
    """Compat: upload de foto de entrega (EntregaFoto). Delega pra upload_publico.

    Mantida por compat com `app/blueprints/driver/routes.py:309`.
    """
    hoje = _agora_brt().strftime('%Y-%m-%d')
    nome = f"{atribuicao_id}_{uuid.uuid4().hex[:8]}.{ext}"
    path = f"{_pasta_base()}/{hoje}/{nome}"
    return upload_publico(file_bytes, path, mode='add', autorename=True)


def _criar_shared_link(token, path):
    """Tenta criar shared link. Se ja existir, busca o existente."""
    r = requests.post(
        'https://api.dropboxapi.com/2/sharing/create_shared_link_with_settings',
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        },
        json={'path': path, 'settings': {'requested_visibility': 'public'}},
        timeout=15,
    )
    if r.status_code == 200:
        return _converter_para_raw(r.json().get('url') or '')

    # 409 = link ja existe -> busca
    if r.status_code == 409:
        r2 = requests.post(
            'https://api.dropboxapi.com/2/sharing/list_shared_links',
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
            },
            json={'path': path, 'direct_only': True},
            timeout=15,
        )
        if r2.status_code == 200:
            links = r2.json().get('links') or []
            if links:
                return _converter_para_raw(links[0].get('url') or '')

    logger.warning('Dropbox shared_link falhou: %s %s', r.status_code, r.text[:200])
    raise RuntimeError(f'create_shared_link falhou: {r.status_code}')


def _converter_para_raw(url):
    """Normaliza URL Dropbox pra servir arquivo raw.

    URLs modernas do Dropbox (formato /scl/fi/...) chegam com `&dl=0` por
    default — preview HTML, nao serve o arquivo. Trocar por `raw=1` serve
    bytes diretos via CDN.

    Robusto contra: dl em qualquer posicao, raw=1 ja presente, duplicatas.
    """
    if not url:
        return url
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

    parsed = urlparse(url)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    # Remove qualquer dl=X (Dropbox prioriza dl sobre raw — se houver dl=0
    # com raw=1, Dropbox serve preview HTML).
    params = [(k, v) for k, v in params if k != 'dl']
    # Remove raw existente (pra evitar duplicatas) e garante exatamente 1.
    params = [(k, v) for k, v in params if k != 'raw']
    params.append(('raw', '1'))
    new_query = urlencode(params)
    return urlunparse(parsed._replace(query=new_query))


_UPLOAD_LIMITE_SIMPLES = 140 * 1024 * 1024  # acima disso, usa upload_session
_CHUNK_SIZE = 100 * 1024 * 1024


def upload_arquivo(file_bytes, dropbox_path):
    """Faz upload generico de bytes pra um caminho Dropbox arbitrario.

    Diferente de upload_foto, nao cria shared link nem prefixa pasta base.
    Usado pra backups, exports, qualquer arquivo que so o admin acessa.

    Acima de 140MB, usa upload_session em chunks de 100MB (limite da API).

    Levanta RuntimeError se nao configurado ou se a API falhar.
    Retorna {'storage_path': str, 'tamanho': int}.
    """
    if not file_bytes:
        raise RuntimeError('Arquivo vazio')
    if not dropbox_path or not dropbox_path.startswith('/'):
        raise RuntimeError('dropbox_path deve comecar com /')

    tamanho = len(file_bytes)
    if tamanho <= _UPLOAD_LIMITE_SIMPLES:
        meta = _upload_simples(file_bytes, dropbox_path)
    else:
        meta = _upload_session(file_bytes, dropbox_path)

    return {
        'storage_path': meta.get('path_lower') or dropbox_path,
        'tamanho': meta.get('size') or tamanho,
    }


def _upload_simples(file_bytes, dropbox_path):
    api_args = {
        'path': dropbox_path,
        'mode': 'overwrite',
        'autorename': False,
        'mute': True,
    }

    def _do_upload():
        token = _token()
        if not token:
            raise RuntimeError('Dropbox nao configurado')
        return requests.post(
            'https://content.dropboxapi.com/2/files/upload',
            headers={
                'Authorization': f'Bearer {token}',
                'Dropbox-API-Arg': json.dumps(api_args),
                'Content-Type': 'application/octet-stream',
            },
            data=file_bytes,
            timeout=120,
        )

    r = _do_upload()
    if r.status_code == 401:
        _invalidar_cache()
        r = _do_upload()
    if r.status_code != 200:
        logger.warning('Dropbox upload falhou: %s %s', r.status_code, r.text[:200])
        raise RuntimeError(f'Upload Dropbox falhou: {r.status_code}')
    return r.json()


def _upload_session(file_bytes, dropbox_path):
    """Upload em chunks pra arquivos > 140MB."""
    token = _token()
    if not token:
        raise RuntimeError('Dropbox nao configurado')

    tamanho = len(file_bytes)
    # 1. start (primeiro chunk)
    primeiro = file_bytes[:_CHUNK_SIZE]
    r = requests.post(
        'https://content.dropboxapi.com/2/files/upload_session/start',
        headers={
            'Authorization': f'Bearer {token}',
            'Dropbox-API-Arg': json.dumps({'close': False}),
            'Content-Type': 'application/octet-stream',
        },
        data=primeiro,
        timeout=300,
    )
    if r.status_code != 200:
        logger.warning('upload_session/start falhou: %s %s', r.status_code, r.text[:200])
        raise RuntimeError(f'Upload Dropbox falhou: {r.status_code}')
    session_id = r.json()['session_id']
    offset = len(primeiro)

    # 2. append + finish
    while offset < tamanho:
        chunk = file_bytes[offset:offset + _CHUNK_SIZE]
        ultimo = (offset + len(chunk)) >= tamanho
        if not ultimo:
            r = requests.post(
                'https://content.dropboxapi.com/2/files/upload_session/append_v2',
                headers={
                    'Authorization': f'Bearer {token}',
                    'Dropbox-API-Arg': json.dumps({
                        'cursor': {'session_id': session_id, 'offset': offset},
                        'close': False,
                    }),
                    'Content-Type': 'application/octet-stream',
                },
                data=chunk,
                timeout=300,
            )
            if r.status_code != 200:
                logger.warning('upload_session/append falhou: %s %s', r.status_code, r.text[:200])
                raise RuntimeError(f'Upload Dropbox falhou: {r.status_code}')
            offset += len(chunk)
        else:
            r = requests.post(
                'https://content.dropboxapi.com/2/files/upload_session/finish',
                headers={
                    'Authorization': f'Bearer {token}',
                    'Dropbox-API-Arg': json.dumps({
                        'cursor': {'session_id': session_id, 'offset': offset},
                        'commit': {
                            'path': dropbox_path,
                            'mode': 'overwrite',
                            'autorename': False,
                            'mute': True,
                        },
                    }),
                    'Content-Type': 'application/octet-stream',
                },
                data=chunk,
                timeout=300,
            )
            if r.status_code != 200:
                logger.warning('upload_session/finish falhou: %s %s', r.status_code, r.text[:200])
                raise RuntimeError(f'Upload Dropbox falhou: {r.status_code}')
            return r.json()
    raise RuntimeError('upload_session: chunks acabaram sem finish?')


def deletar(storage_path):
    """Best-effort: deleta arquivo do Dropbox. Usado quando admin remove foto."""
    token = _token()
    if not token or not storage_path:
        return False
    try:
        r = requests.post(
            'https://api.dropboxapi.com/2/files/delete_v2',
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
            },
            json={'path': storage_path},
            timeout=15,
        )
        return r.status_code == 200
    except Exception:
        logger.exception('Erro deletando foto do Dropbox')
        return False


def listar_pasta(path):
    """Lista arquivos de uma pasta do Dropbox (nao-recursivo, com paginacao).

    Retorna lista de {'path': str, 'nome': str, 'tamanho': int,
    'modificado': str ISO-8601} so de ARQUIVOS (ignora subpastas), ou []
    se pasta inexistente/erro. Usado pela retencao de backups e pelo drill
    de restore (achar o dump mais recente)."""
    token = _token()
    if not token or not path:
        return []
    out = []
    url = 'https://api.dropboxapi.com/2/files/list_folder'
    body = {'path': path.rstrip('/'), 'recursive': False, 'limit': 500}
    try:
        while True:
            r = requests.post(
                url,
                headers={'Authorization': f'Bearer {token}',
                         'Content-Type': 'application/json'},
                json=body,
                timeout=30,
            )
            if r.status_code == 401:
                _invalidar_cache()
                token = _token()
                if not token:
                    return out
                continue
            if r.status_code != 200:
                # path/not_found = pasta nunca criada (sem backups ainda): nao
                # eh erro de verdade, devolve vazio sem poluir o log.
                if 'not_found' not in (r.text or ''):
                    logger.warning('Dropbox list_folder %s: %s %s',
                                   path, r.status_code, r.text[:200])
                return out
            data = r.json()
            for e in data.get('entries', []):
                if e.get('.tag') != 'file':
                    continue
                out.append({
                    'path': e.get('path_lower') or e.get('path_display') or '',
                    'nome': e.get('name') or '',
                    'tamanho': e.get('size') or 0,
                    'modificado': e.get('server_modified') or '',
                })
            if not data.get('has_more'):
                return out
            url = 'https://api.dropboxapi.com/2/files/list_folder/continue'
            body = {'cursor': data.get('cursor')}
    except requests.RequestException:
        logger.exception('Erro listando pasta do Dropbox %s', path)
        return out


# Causa da ultima falha de `baixar()` (thread-local, padrao do tiny.py):
# o drill consome pra reportar a causa EXATA no relatorio — sem isso, o
# motivo era so "download falhou" e o debug exigia acesso aos logs do
# Railway (visto em prod 2026-06-09).
import threading as _threading

_falha_download = _threading.local()


def consumir_falha_download():
    motivo = getattr(_falha_download, 'motivo', None)
    _falha_download.motivo = None
    return motivo


def baixar(path):
    """Baixa um arquivo do Dropbox. Retorna bytes ou None (causa em
    `consumir_falha_download()`).

    Usado pelo drill de restore (puxar o ultimo dump pra validar)."""
    token = _token()
    if not token or not path:
        _falha_download.motivo = 'Dropbox nao configurado ou path vazio'
        return None

    def _do():
        return requests.post(
            'https://content.dropboxapi.com/2/files/download',
            headers={'Authorization': f'Bearer {token}',
                     'Dropbox-API-Arg': json.dumps({'path': path})},
            timeout=300,   # dumps de ~100MB em link lento
        )

    try:
        r = _do()
        if r.status_code == 401:
            _invalidar_cache()
            token = _token()
            if not token:
                _falha_download.motivo = 'token expirou e refresh falhou'
                return None
            r = _do()
        if r.status_code != 200:
            corpo = (r.text or '')[:200]
            _falha_download.motivo = f'HTTP {r.status_code}: {corpo}'
            logger.warning('Dropbox download %s: %s %s',
                           path, r.status_code, corpo)
            return None
        return r.content
    except requests.RequestException as exc:
        _falha_download.motivo = f'{type(exc).__name__}: {exc}'
        logger.exception('Erro baixando %s do Dropbox', path)
        return None

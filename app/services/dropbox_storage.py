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


def upload_foto(file_bytes, atribuicao_id, ext='jpg'):
    """Faz upload da foto e retorna {'url': str, 'storage_path': str, 'tamanho': int}.

    Levanta RuntimeError se nao configurado ou se a API falhar.
    """
    if not file_bytes:
        raise RuntimeError('Arquivo vazio')

    # Caminho organizado por data + atribuicao + uuid pra evitar colisao
    hoje = _agora_brt().strftime('%Y-%m-%d')
    nome = f"{atribuicao_id}_{uuid.uuid4().hex[:8]}.{ext}"
    path = f"{_pasta_base()}/{hoje}/{nome}"

    api_args = {
        'path': path,
        'mode': 'add',
        'autorename': True,
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
    if r.status_code == 401:  # token expirado mid-request
        _invalidar_cache()
        r = _do_upload()
    if r.status_code != 200:
        logger.warning('Dropbox upload falhou: %s %s', r.status_code, r.text[:200])
        raise RuntimeError(f'Upload Dropbox falhou: {r.status_code}')

    meta = r.json()
    storage_path = meta.get('path_lower') or path
    url = _criar_shared_link(_token(), storage_path)

    return {
        'url': url,
        'storage_path': storage_path,
        'tamanho': meta.get('size') or len(file_bytes),
    }


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
    """Converte URL ?dl=0 do Dropbox em ?raw=1 que serve o arquivo direto."""
    if not url:
        return url
    if '?dl=0' in url:
        return url.replace('?dl=0', '?raw=1')
    if '?dl=1' in url:
        return url.replace('?dl=1', '?raw=1')
    sep = '&' if '?' in url else '?'
    return f"{url}{sep}raw=1"


def upload_arquivo(file_bytes, dropbox_path):
    """Faz upload generico de bytes pra um caminho Dropbox arbitrario.

    Diferente de upload_foto, nao cria shared link nem prefixa pasta base.
    Usado pra backups, exports, qualquer arquivo que so o admin acessa.

    Levanta RuntimeError se nao configurado ou se a API falhar.
    Retorna {'storage_path': str, 'tamanho': int}.
    """
    if not file_bytes:
        raise RuntimeError('Arquivo vazio')
    if not dropbox_path or not dropbox_path.startswith('/'):
        raise RuntimeError('dropbox_path deve comecar com /')

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
            timeout=120,  # backup pode ser grande
        )

    r = _do_upload()
    if r.status_code == 401:
        _invalidar_cache()
        r = _do_upload()
    if r.status_code != 200:
        logger.warning('Dropbox upload_arquivo falhou: %s %s', r.status_code, r.text[:200])
        raise RuntimeError(f'Upload Dropbox falhou: {r.status_code}')

    meta = r.json()
    return {
        'storage_path': meta.get('path_lower') or dropbox_path,
        'tamanho': meta.get('size') or len(file_bytes),
    }


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

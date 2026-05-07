"""Upload de fotos de entrega no Dropbox.

Usa a Dropbox HTTP API direto (sem SDK) pra evitar dependencia extra.
Fluxo:
1. Faz upload via /2/files/upload -> retorna metadata (path)
2. Cria/recupera link compartilhavel via /2/sharing/create_shared_link_with_settings
3. Converte URL "?dl=0" pra "?raw=1" pra servir bytes direto

Config necessaria:
- DROPBOX_ACCESS_TOKEN: token gerado no painel da Dropbox app
  (escopo: files.content.write, sharing.write)
- DROPBOX_PASTA_BASE: pasta dentro do Dropbox onde salvar (default /Apps/Receitas-Entregas)
"""
import json
import logging
import uuid
from datetime import datetime

import requests
from flask import current_app

logger = logging.getLogger(__name__)


def _token():
    return (current_app.config.get('DROPBOX_ACCESS_TOKEN') or '').strip()


def _pasta_base():
    return (current_app.config.get('DROPBOX_PASTA_BASE') or '/Apps/Receitas-Entregas').rstrip('/')


def disponivel():
    return bool(_token())


def upload_foto(file_bytes, atribuicao_id, ext='jpg'):
    """Faz upload da foto e retorna {'url': str, 'storage_path': str, 'tamanho': int}.

    Levanta RuntimeError se nao configurado ou se a API falhar.
    """
    token = _token()
    if not token:
        raise RuntimeError('Dropbox nao configurado (DROPBOX_ACCESS_TOKEN ausente)')
    if not file_bytes:
        raise RuntimeError('Arquivo vazio')

    # Caminho organizado por data + atribuicao + uuid pra evitar colisao
    hoje = datetime.utcnow().strftime('%Y-%m-%d')
    nome = f"{atribuicao_id}_{uuid.uuid4().hex[:8]}.{ext}"
    path = f"{_pasta_base()}/{hoje}/{nome}"

    # 1. Upload
    api_args = {
        'path': path,
        'mode': 'add',
        'autorename': True,
        'mute': True,
    }
    r = requests.post(
        'https://content.dropboxapi.com/2/files/upload',
        headers={
            'Authorization': f'Bearer {token}',
            'Dropbox-API-Arg': json.dumps(api_args),
            'Content-Type': 'application/octet-stream',
        },
        data=file_bytes,
        timeout=30,
    )
    if r.status_code != 200:
        logger.warning('Dropbox upload falhou: %s %s', r.status_code, r.text[:200])
        raise RuntimeError(f'Upload Dropbox falhou: {r.status_code}')

    meta = r.json()
    storage_path = meta.get('path_lower') or path

    # 2. Cria shared link
    url = _criar_shared_link(token, storage_path)

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

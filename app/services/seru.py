"""Cliente da API Seru (PDV).

Doc: https://integration.plataformaseru.com.br/v1/docs

Autenticação OAuth2 client_credentials. Token tem expiração; cachemos em
memória do processo (com pequeno safety margin) e renovamos sob demanda.
"""
import base64
import logging
import time

import requests
from flask import current_app

logger = logging.getLogger(__name__)

BASE = 'https://integration.plataformaseru.com.br/v1'

# Cache do token entre requests do mesmo worker. Se expira, renovamos.
_token_cache = {'access_token': None, 'expires_at': 0}


def _credenciais():
    cid = (current_app.config.get('SERU_CLIENT_ID') or '').strip()
    secret = (current_app.config.get('SERU_CLIENT_SECRET') or '').strip()
    return cid, secret


def _obter_token(force_refresh=False):
    """Retorna access token valido, renovando quando necessario."""
    cid, secret = _credenciais()
    if not cid or not secret:
        raise RuntimeError('SERU_CLIENT_ID/SERU_CLIENT_SECRET nao configurados.')

    agora = time.time()
    if not force_refresh and _token_cache['access_token'] and agora < _token_cache['expires_at'] - 30:
        return _token_cache['access_token']

    basic = base64.b64encode(f'{cid}:{secret}'.encode()).decode()
    r = requests.post(
        f'{BASE}/oauth/token',
        headers={'Authorization': f'Basic {basic}', 'Content-Type': 'application/json'},
        json={'grantType': 'client_credentials'},
        timeout=20,
    )
    if r.status_code not in (200, 201):
        logger.error('Seru auth falhou %s: %s', r.status_code, r.text[:300])
        raise RuntimeError(f'Seru auth {r.status_code}: {r.text[:200]}')
    body = r.json()
    _token_cache['access_token'] = body.get('accessToken')
    expires_in = int(body.get('expiresIn') or 3600)
    _token_cache['expires_at'] = agora + expires_in
    return _token_cache['access_token']


def _get(path, params=None):
    """GET autenticado. Renova token automaticamente em 401."""
    token = _obter_token()
    r = requests.get(f'{BASE}{path}',
                     headers={'Authorization': f'Bearer {token}'},
                     params=params or {}, timeout=20)
    if r.status_code == 401:
        token = _obter_token(force_refresh=True)
        r = requests.get(f'{BASE}{path}',
                         headers={'Authorization': f'Bearer {token}'},
                         params=params or {}, timeout=20)
    if r.status_code != 200:
        logger.error('Seru %s %s: %s', path, r.status_code, r.text[:300])
        raise RuntimeError(f'Seru {path} {r.status_code}: {r.text[:200]}')
    return r.json()


def _iso_dia(data, fim=False):
    """Converte uma date pra ISO 8601 UTC. Fim do dia = 23:59:59Z."""
    if fim:
        return data.strftime('%Y-%m-%dT23:59:59Z')
    return data.strftime('%Y-%m-%dT00:00:00Z')


def listar_pedidos(data_inicial, data_final, page=1, limit=100, hasCanceledItem=None,
                   initial_updated_at=None, final_updated_at=None):
    """Lista pedidos da Seru. Por padrao filtra por updatedAt no intervalo
    [data_inicial, data_final], mas o caller pode sobrescrever passando
    initial_updated_at/final_updated_at (objetos date).

    Retorna dict {success, page, limit, totalPages, data: [...]}
    """
    iu = initial_updated_at or data_inicial
    fu = final_updated_at or data_final
    params = {
        'initialUpdatedAt': _iso_dia(iu, fim=False),
        'finalUpdatedAt': _iso_dia(fu, fim=True),
        'page': page,
        'limit': limit,
    }
    if hasCanceledItem is not None:
        params['hasCanceledItem'] = 'true' if hasCanceledItem else 'false'
    return _get('/orders', params=params)


def listar_pedidos_completo(data_inicial, data_final, expandir_dias_frente=0):
    """Itera todas as páginas e devolve uma lista única.

    A Seru limita cada chamada a uma janela de 24h em updatedAt. Pra cobrir
    intervalos maiores ou pegar pedidos atualizados depois, fazemos uma chamada
    POR DIA desde data_inicial ate data_final + expandir_dias_frente.

    Caller deve filtrar pelo createdAt depois pra precisao.
    """
    from datetime import timedelta
    fim_busca = data_final + timedelta(days=expandir_dias_frente)
    todos = []
    dia = data_inicial
    dias_consultados = 0
    while dia <= fim_busca:
        page = 1
        while True:
            r = listar_pedidos(dia, dia, page=page, limit=100)
            todos.extend(r.get('data') or [])
            total = r.get('totalPages') or 1
            if page >= total:
                break
            page += 1
            if page > 50:
                logger.warning('Seru listar_pedidos: parando em 50 paginas no dia %s', dia)
                break
        dias_consultados += 1
        if dias_consultados > 60:  # safety
            logger.warning('Seru listar_pedidos: parando em 60 dias')
            break
        dia += timedelta(days=1)
    return todos


def detalhes_pedido(pedido_id):
    return _get(f'/orders/{pedido_id}')

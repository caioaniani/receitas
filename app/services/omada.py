"""Ponte com o controlador Omada (OC200) via Open API — nuvem TP-Link.

Papel único: AUTORIZAR o aparelho do cliente no hotspot depois que o portal
Wi-Fi validou o cadastro (wifi_portal.py). O OC200 da Ribeiro do Vale tem
Cloud Access ligado; a Open API (controller v5.9+) permite chamar o
controlador pela nuvem sem tocar na rede da loja (Vivo Fibra/CGNAT).

Config (Railway): OMADA_API_URL (ex: https://use1-omada-northbound.
tplinkcloud.com), OMADA_CLIENT_ID/OMADA_CLIENT_SECRET (criados em
Settings → Platform Integration → Open API no Omada), OMADA_OMADAC_ID
(id do controlador na nuvem) e OMADA_SITE_ID (site "ribeiro do vale").

Enquanto as envs não estiverem setadas, `autorizar_cliente` devolve
{'ok': False, 'erro': 'nao_configurado'} e o portal segue funcionando SEM
enforcement (cadastro/login normais; o Wi-Fi já está aberto na rede de
clientes). O token OAuth (client_credentials) é cacheado em memória por
worker, mesmo padrão do seru.py.
"""
import logging
import time

import requests
from flask import current_app

logger = logging.getLogger(__name__)

_token_cache = {'token': None, 'expira': 0}


def disponivel():
    cfg = current_app.config
    return all((cfg.get(k) or '').strip() for k in (
        'OMADA_API_URL', 'OMADA_CLIENT_ID', 'OMADA_CLIENT_SECRET',
        'OMADA_OMADAC_ID', 'OMADA_SITE_ID'))


def _base():
    return (current_app.config.get('OMADA_API_URL') or '').strip().rstrip('/')


def _token():
    """Access token client_credentials, cacheado até ~1 min antes de expirar."""
    agora_ts = time.time()
    if _token_cache['token'] and _token_cache['expira'] > agora_ts + 60:
        return _token_cache['token']
    cfg = current_app.config
    r = requests.post(
        f'{_base()}/openapi/authorize/token',
        params={'grant_type': 'client_credentials'},
        json={'omadacId': (cfg.get('OMADA_OMADAC_ID') or '').strip(),
              'client_id': (cfg.get('OMADA_CLIENT_ID') or '').strip(),
              'client_secret': (cfg.get('OMADA_CLIENT_SECRET') or '').strip()},
        timeout=15)
    data = r.json() if r.text else {}
    token = ((data.get('result') or {}).get('accessToken')
             if isinstance(data, dict) else None)
    if r.status_code != 200 or not token:
        raise RuntimeError(f'token Omada falhou: HTTP {r.status_code} '
                           f'{(r.text or "")[:200]}')
    _token_cache['token'] = token
    _token_cache['expira'] = agora_ts + int(
        (data.get('result') or {}).get('expiresIn') or 7200)
    return token


def autorizar_cliente(client_mac, ap_mac=None, ssid=None, minutos=1440):
    """Autoriza o MAC do cliente no hotspot do site por `minutos`.

    Retorna {'ok': bool, 'erro': str|None}. NUNCA levanta — o portal trata
    a falha como 'autorização pendente' (o cadastro do cliente não pode
    morrer por causa do controlador fora do ar)."""
    if not disponivel():
        return {'ok': False, 'erro': 'nao_configurado'}
    if not (client_mac or '').strip():
        return {'ok': False, 'erro': 'client_mac ausente'}
    cfg = current_app.config
    omadac = (cfg.get('OMADA_OMADAC_ID') or '').strip()
    site = (cfg.get('OMADA_SITE_ID') or '').strip()
    try:
        token = _token()
        # Endpoint de autorização do hotspot (Open API v5.x). O formato do
        # MAC que a API espera é AA-BB-CC-DD-EE-FF.
        mac = client_mac.replace(':', '-').upper()
        r = requests.post(
            f'{_base()}/openapi/v1/{omadac}/sites/{site}/hotspot/'
            'extPortal/auth',
            headers={'Authorization': f'AccessToken={token}',
                     'Content-Type': 'application/json'},
            json={'clientMac': mac,
                  'apMac': (ap_mac or '').replace(':', '-').upper() or None,
                  'ssidName': ssid or None,
                  'time': int(minutos) * 60 * 1000,   # ms de acesso
                  'authType': 4},
            timeout=15)
        data = r.json() if r.text else {}
        cod = data.get('errorCode') if isinstance(data, dict) else None
        if r.status_code == 200 and cod in (0, None):
            return {'ok': True, 'erro': None}
        erro = f'HTTP {r.status_code} errorCode={cod} {(r.text or "")[:200]}'
        logger.warning('omada autorizar_cliente: %s', erro)
        return {'ok': False, 'erro': erro}
    except Exception as exc:  # noqa: BLE001
        logger.exception('omada autorizar_cliente falhou')
        return {'ok': False, 'erro': str(exc)[:200]}

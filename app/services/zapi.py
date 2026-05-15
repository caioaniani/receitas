"""Cliente Z-API (WhatsApp) — envia mensagens de texto.

Cadastro/setup: https://z-api.io/
- Cria instancia, conecta WhatsApp via QR code, copia INSTANCE_ID + TOKEN
- Opcional: ativa Token de Seguranca (Client-Token) em Account Settings

Env vars: ZAPI_INSTANCE_ID, ZAPI_TOKEN, ZAPI_CLIENT_TOKEN (opcional).
"""
import logging

import requests
from flask import current_app

logger = logging.getLogger(__name__)

BASE = 'https://api.z-api.io'


def disponivel():
    cfg = current_app.config
    return bool((cfg.get('ZAPI_INSTANCE_ID') or '').strip()
                and (cfg.get('ZAPI_TOKEN') or '').strip())


def _normalizar_numero(numero):
    """Mantem so digitos. '+55 11 99999-9999' → '5511999999999'."""
    return ''.join(c for c in (numero or '') if c.isdigit())


def enviar_texto(numero, mensagem):
    """POST /send-text com texto simples. Retorna {'ok': bool, ...}."""
    cfg = current_app.config
    instance_id = (cfg.get('ZAPI_INSTANCE_ID') or '').strip()
    token = (cfg.get('ZAPI_TOKEN') or '').strip()
    client_token = (cfg.get('ZAPI_CLIENT_TOKEN') or '').strip()

    if not instance_id or not token:
        return {'ok': False, 'erro': 'Z-API nao configurado (ZAPI_INSTANCE_ID/ZAPI_TOKEN)'}

    numero_norm = _normalizar_numero(numero)
    if not numero_norm:
        return {'ok': False, 'erro': 'numero invalido'}

    url = f'{BASE}/instances/{instance_id}/token/{token}/send-text'
    headers = {'Content-Type': 'application/json'}
    if client_token:
        headers['Client-Token'] = client_token

    try:
        r = requests.post(url, json={'phone': numero_norm, 'message': mensagem or ''},
                          headers=headers, timeout=15)
        if r.status_code not in (200, 201):
            logger.warning('zapi send-text %s: %s', r.status_code, r.text[:200])
            return {'ok': False, 'erro': f'HTTP {r.status_code}: {r.text[:200]}'}
        return {'ok': True, 'response': r.json() if r.text else {}}
    except Exception as exc:  # noqa: BLE001
        logger.exception('zapi enviar_texto falhou')
        return {'ok': False, 'erro': str(exc)}

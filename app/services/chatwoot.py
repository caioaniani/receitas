"""Cliente fino da API do Chatwoot.

O Chatwoot é o inbox omnichannel (self-hosted, Railway). Este módulo só
EMPURRA dados pra lá — hoje, opcionalmente, atributos do contato (ex:
"Cliente B2B: Zion", "Débito: R$ 120,00") pra o atendente ver sem abrir o
card iframe. O recebimento de mensagens é responsabilidade do Chatwoot, não
deste sistema.

Config: CHATWOOT_URL, CHATWOOT_API_TOKEN, CHATWOOT_ACCOUNT_ID.
Fonte canônica de normalização de telefone: app.utils.telefone_chave.
"""
import logging

import requests
from flask import current_app

logger = logging.getLogger(__name__)


def disponivel():
    cfg = current_app.config
    return bool((cfg.get('CHATWOOT_URL') or '').strip()
                and (cfg.get('CHATWOOT_API_TOKEN') or '').strip()
                and (cfg.get('CHATWOOT_ACCOUNT_ID') or '').strip())


def _base():
    cfg = current_app.config
    url = (cfg.get('CHATWOOT_URL') or '').strip().rstrip('/')
    acc = (cfg.get('CHATWOOT_ACCOUNT_ID') or '').strip()
    return f'{url}/api/v1/accounts/{acc}'


def _headers():
    return {'api_access_token': (current_app.config.get('CHATWOOT_API_TOKEN') or '').strip(),
            'Content-Type': 'application/json'}


def atualizar_atributos_contato(contact_id, atributos):
    """PUT custom_attributes num contato existente. Retorna {'ok': bool}.

    `atributos` é um dict (ex: {'cliente_b2b': 'Zion', 'debito': '120.00'}).
    Best-effort: erro de rede não deve quebrar o fluxo de quem chama.
    """
    if not disponivel():
        return {'ok': False, 'erro': 'Chatwoot não configurado'}
    url = f'{_base()}/contacts/{contact_id}'
    try:
        r = requests.put(url, json={'custom_attributes': atributos},
                         headers=_headers(), timeout=10)
        if r.status_code not in (200, 201):
            logger.warning('chatwoot update contato %s: %s', r.status_code, r.text[:200])
            return {'ok': False, 'erro': f'HTTP {r.status_code}'}
        return {'ok': True}
    except Exception as exc:  # noqa: BLE001
        logger.exception('chatwoot atualizar_atributos_contato falhou')
        return {'ok': False, 'erro': str(exc)}

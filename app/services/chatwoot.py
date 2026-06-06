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


# ── Agent Bot (atendimento automatico) ──
# Usa o token do Agent Bot (CHATWOOT_BOT_TOKEN), nao o de usuario, pra as
# mensagens aparecerem como do bot. Webhook em app/blueprints/crm/routes.py.


def bot_disponivel():
    cfg = current_app.config
    return bool((cfg.get('CHATWOOT_URL') or '').strip()
                and (cfg.get('CHATWOOT_BOT_TOKEN') or '').strip()
                and (cfg.get('CHATWOOT_ACCOUNT_ID') or '').strip())


def _bot_headers():
    return {'api_access_token': (current_app.config.get('CHATWOOT_BOT_TOKEN') or '').strip(),
            'Content-Type': 'application/json'}


def enviar_mensagem(conversation_id, content):
    """Posta uma resposta do bot numa conversa. Retorna {'ok': bool}."""
    if not bot_disponivel():
        return {'ok': False, 'erro': 'Chatwoot bot nao configurado'}
    url = f'{_base()}/conversations/{conversation_id}/messages'
    try:
        r = requests.post(url, json={'content': content, 'message_type': 'outgoing'},
                          headers=_bot_headers(), timeout=10)
        if r.status_code not in (200, 201):
            logger.warning('chatwoot enviar_mensagem %s: %s', r.status_code, r.text[:200])
            return {'ok': False, 'erro': f'HTTP {r.status_code}'}
        return {'ok': True}
    except Exception as exc:  # noqa: BLE001
        logger.exception('chatwoot enviar_mensagem falhou')
        return {'ok': False, 'erro': str(exc)}


def definir_status(conversation_id, status):
    """Muda o status da conversa. 'open' = passa pro humano (sai do bot);
    'pending' = devolve pro bot; 'resolved' = encerra."""
    if not bot_disponivel():
        return {'ok': False, 'erro': 'Chatwoot bot nao configurado'}
    url = f'{_base()}/conversations/{conversation_id}/toggle_status'
    try:
        r = requests.post(url, json={'status': status},
                          headers=_bot_headers(), timeout=10)
        if r.status_code not in (200, 201):
            logger.warning('chatwoot definir_status %s: %s', r.status_code, r.text[:200])
            return {'ok': False, 'erro': f'HTTP {r.status_code}'}
        return {'ok': True}
    except Exception as exc:  # noqa: BLE001
        logger.exception('chatwoot definir_status falhou')
        return {'ok': False, 'erro': str(exc)}


def buscar_historico(conversation_id, limite=20):
    """Mensagens recentes da conversa, em ordem cronologica, mapeadas pra
    [{'role': 'user'|'assistant', 'content': str, 'imagens'?: [url]}] (pro
    Claude). Cliente = user (incoming), bot/atendente = assistant (outgoing).
    Ignora notas internas e eventos. Anexos de imagem do cliente entram em
    'imagens' (URLs do Chatwoot) — quem monta o prompt baixa via baixar_imagem.
    Mensagem so-imagem (sem texto) do cliente tambem entra."""
    if not bot_disponivel():
        return []
    url = f'{_base()}/conversations/{conversation_id}/messages'
    try:
        r = requests.get(url, headers=_bot_headers(), timeout=10)
        if r.status_code not in (200, 201):
            return []
        data = r.json() if r.text else {}
    except Exception:  # noqa: BLE001
        logger.exception('chatwoot buscar_historico falhou')
        return []

    msgs = data.get('payload') if isinstance(data, dict) else data
    if not isinstance(msgs, list):
        return []
    msgs = sorted(msgs, key=lambda m: m.get('created_at') or 0)

    hist = []
    for m in msgs:
        if m.get('private'):
            continue
        content = (m.get('content') or '').strip()
        if not content:
            continue
        mt = m.get('message_type')
        if mt in ('incoming', 0):
            hist.append({'role': 'user', 'content': content})
        elif mt in ('outgoing', 1):
            hist.append({'role': 'assistant', 'content': content})
    return hist[-limite:]

"""Integração com o Pagar.me (Stone) — API Core v5 (Fase 4 loja online).

Autenticação: Basic auth com a SECRET KEY como usuário e senha vazia
(`Authorization: Basic base64(sk_...:)`). Base: https://api.pagar.me/core/v5.

Esta primeira parte cobre só o que NÃO move dinheiro: checar se a chave
configurada é válida (rota /admin/debug-pagarme). Criação de pedido (Pix/
cartão) e webhook entram na sequência, com testes em sandbox antes de
qualquer cobrança real.

SEGURANÇA: a chave NUNCA vem pelo chat — o dono cadastra no Railway. O
código só lê via config. As funções são best-effort e não levantam exceção
pro caller; devolvem dict {'ok': ...}.
"""
import base64
import logging

import requests
from flask import current_app

logger = logging.getLogger(__name__)

_BASE = 'https://api.pagar.me/core/v5'
_TIMEOUT = 20


def _chave():
    return (current_app.config.get('PAGARME_API_KEY') or '').strip()


def disponivel():
    return bool(_chave())


def ambiente():
    """'sandbox' | 'producao' | 'desconhecido' a partir do prefixo da chave
    (sk_test_ = sandbox, sk_live_ = produção). Não expõe o segredo."""
    sk = _chave()
    if sk.startswith('sk_test_'):
        return 'sandbox'
    if sk.startswith('sk_live_'):
        return 'producao'
    return 'desconhecido'


def _headers():
    token = base64.b64encode(f'{_chave()}:'.encode()).decode()
    return {
        'Authorization': f'Basic {token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }


def validar_chave():
    """Faz uma chamada autenticada leve pra confirmar que a chave funciona.
    Não cria nada nem expõe a chave. Retorna {'ok': bool, ...}."""
    if not disponivel():
        return {'ok': False, 'erro': 'PAGARME_API_KEY não configurada'}
    try:
        r = requests.get(f'{_BASE}/customers?size=1',
                         headers=_headers(), timeout=_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        logger.warning('pagarme validar_chave falhou: %s', exc)
        return {'ok': False, 'erro': str(exc)}
    if r.status_code in (200, 201):
        return {'ok': True, 'ambiente': ambiente()}
    if r.status_code in (401, 403):
        return {'ok': False, 'erro': f'chave recusada pelo Pagar.me ({r.status_code})'}
    detalhe = (r.text or '')[:200]
    return {'ok': False, 'erro': f'resposta inesperada ({r.status_code}): {detalhe}'}

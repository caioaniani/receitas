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


# ── Helpers de conversão (DINHEIRO) ───────────────────────────────────
# Pagar.me v5 usa CENTAVOS (inteiro) em todos os campos `amount`. Nossas
# tabelas guardam Decimal/Numeric(10,2). Convertemos AQUI, em UMA função
# só, pra não espalhar truque de centavo por todo lado.

def _centavos(valor):
    """Decimal/float/str -> centavos (int). Arredonda no padrão monetário
    (HALF_UP). Lança ValueError se valor inválido."""
    from decimal import ROUND_HALF_UP, Decimal
    if valor is None:
        return 0
    d = (Decimal(str(valor)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    return int(d)


# ── Payloads (PedidoOnline -> Pagar.me v5) ────────────────────────────

def _so_digitos(s):
    return ''.join(c for c in (s or '') if c.isdigit())


def _payload_customer(pedido):
    """Customer (cliente final). E-mail é obrigatório; document/phones se
    tiverem. Pagar.me usa esse customer pra associar e contatar."""
    payload = {
        'name': pedido.nome_cliente,
        'email': pedido.email_cliente,
        'type': 'individual',
    }
    cli = getattr(pedido, 'cliente', None)
    cpf = _so_digitos(getattr(cli, 'cpf', '') if cli else '')
    if cpf:
        payload['document'] = cpf
        payload['document_type'] = 'cpf'
    tel = _so_digitos(pedido.telefone_cliente or '')
    if len(tel) >= 10:
        ddd, num = tel[:2], tel[2:]
        payload['phones'] = {
            'mobile_phone': {
                'country_code': '55', 'area_code': ddd, 'number': num,
            },
        }
    return payload


def _payload_items(pedido):
    """Items da v5: amount em centavos, quantity inteiro. `code` ajuda a
    rastrear no painel (kind+id)."""
    out = []
    for it in pedido.itens:
        out.append({
            'amount': _centavos(it.preco_unitario),
            'description': it.nome[:255],
            'quantity': int(it.quantidade),
            'code': f'{it.kind}-{it.receita_id or it.produto_id or "?"}',
        })
    # Frete vira um "item" no Pagar.me — o total tem que bater com a soma
    # dos items (não tem campo separado de shipping na v5).
    frete_c = _centavos(pedido.frete_valor)
    if frete_c > 0:
        out.append({'amount': frete_c, 'description': 'Frete',
                    'quantity': 1, 'code': 'frete'})
    return out


def _post_order(payload):
    """POST /core/v5/orders. Retorna (status_code, json_dict)."""
    if not disponivel():
        return 0, {'_erro': 'PAGARME_API_KEY não configurada'}
    try:
        r = requests.post(f'{_BASE}/orders', headers=_headers(),
                          json=payload, timeout=_TIMEOUT)
        try:
            body = r.json() or {}
        except ValueError:
            body = {'_texto': (r.text or '')[:300]}
        return r.status_code, body
    except Exception as exc:  # noqa: BLE001
        logger.warning('pagarme POST /orders falhou: %s', exc)
        return 0, {'_erro': str(exc)}


def _extrair_charge(order_json):
    """Pega a primeira charge do order. Pagar.me retorna `charges: [...]`
    com a transação dentro."""
    charges = (order_json or {}).get('charges') or []
    return charges[0] if charges else {}


def criar_pedido_pix(pedido, expira_em_min=30):
    """Cria um Order no Pagar.me com payment_method=pix. Devolve dict:
      {ok, order_id, charge_id, qr_code, qr_code_url, expira_em, erro?}
    Onde qr_code é o EMV copia-e-cola (texto) e qr_code_url é a imagem
    (se vier). Best-effort: nunca levanta exceção."""
    from datetime import timedelta

    from app.utils import agora
    payload = {
        'customer': _payload_customer(pedido),
        'items': _payload_items(pedido),
        'payments': [{
            'payment_method': 'pix',
            'amount': _centavos(pedido.valor_total),
            'pix': {'expires_in': int(expira_em_min) * 60},
        }],
        'code': pedido.codigo,
    }
    status, body = _post_order(payload)
    if status not in (200, 201):
        erro = body.get('message') or body.get('_erro') or f'HTTP {status}'
        return {'ok': False, 'erro': erro, 'http': status}
    charge = _extrair_charge(body)
    last_tx = charge.get('last_transaction') or {}
    return {
        'ok': True,
        'order_id': body.get('id'),
        'charge_id': charge.get('id'),
        'status': charge.get('status'),
        'qr_code': last_tx.get('qr_code'),
        'qr_code_url': last_tx.get('qr_code_url'),
        'expira_em': agora() + timedelta(minutes=int(expira_em_min)),
    }


def criar_pedido_cartao(pedido, card_token, parcelas=1):
    """Cria Order com payment_method=credit_card usando token tokenizado
    no FRONT (pk_, JS do Pagar.me). O servidor NUNCA vê o número do cartão.
    Devolve {ok, order_id, charge_id, status, erro?}."""
    parcelas = max(1, min(int(parcelas or 1), 12))
    payload = {
        'customer': _payload_customer(pedido),
        'items': _payload_items(pedido),
        'payments': [{
            'payment_method': 'credit_card',
            'amount': _centavos(pedido.valor_total),
            'credit_card': {
                'operation_type': 'auth_and_capture',
                'installments': parcelas,
                'statement_descriptor': 'O PAO PADARIA',
                'card_token': card_token,
            },
        }],
        'code': pedido.codigo,
    }
    status, body = _post_order(payload)
    if status not in (200, 201):
        erro = body.get('message') or body.get('_erro') or f'HTTP {status}'
        return {'ok': False, 'erro': erro, 'http': status}
    charge = _extrair_charge(body)
    return {
        'ok': True,
        'order_id': body.get('id'),
        'charge_id': charge.get('id'),
        'status': charge.get('status'),  # 'paid', 'failed', 'pending'…
    }


def cancelar_charge(charge_id, valor_decimal=None):
    """Cancela/refund de uma cobrança. Se valor_decimal vier, refund
    parcial; senão, total. Devolve {ok, erro?}."""
    if not disponivel():
        return {'ok': False, 'erro': 'PAGARME_API_KEY não configurada'}
    if not charge_id:
        return {'ok': False, 'erro': 'charge_id ausente'}
    payload = {}
    if valor_decimal is not None:
        payload['amount'] = _centavos(valor_decimal)
    try:
        r = requests.delete(f'{_BASE}/charges/{charge_id}',
                            headers=_headers(),
                            json=payload or None, timeout=_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        logger.warning('pagarme cancelar_charge falhou: %s', exc)
        return {'ok': False, 'erro': str(exc)}
    if r.status_code in (200, 201, 202):
        return {'ok': True}
    detalhe = (r.text or '')[:200]
    return {'ok': False, 'erro': f'HTTP {r.status_code}: {detalhe}'}

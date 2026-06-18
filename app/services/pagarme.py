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
    """'sandbox' | 'producao' | 'desconhecido' a partir do prefixo da chave.

    Reconhece 3 formatos do Pagar.me/Stone:
    - `sk_test_*` → sandbox (v5 clássico)
    - `sk_live_*` → produção (v5 clássico)
    - `sk_<hash>` (sem test/live) → produção. Padrão novo confirmado em prod
      18/06/2026 (chave do dono começava com `sk_f2f38…`, gerada no painel
      de produção). Pagar.me não emite chave de sandbox neste formato — a
      sandbox sempre tem `_test_` explícito. Defensivo: só decidimos
      'producao' quando a chave de fato AUTENTICA na API (chamamos a API
      antes em `validar_chave`); aqui apenas classificamos o formato.

    Devolve 'desconhecido' pra formatos não-Pagar.me (chave de outro
    serviço por engano)."""
    sk = _chave()
    if sk.startswith('sk_test_'):
        return 'sandbox'
    if sk.startswith('sk_live_'):
        return 'producao'
    if sk.startswith('sk_'):
        return 'producao'
    return 'desconhecido'


def prefixo_chave():
    """Primeiros 8 chars da SECRET key (`sk_test_`/`sk_live_`/`sk_…`) +
    elipse. NÃO expõe o segredo (o resto é cortado), mas permite ao owner
    confirmar visualmente o ambiente no `/admin/debug-pagarme`."""
    sk = _chave()
    if not sk:
        return ''
    return (sk[:8] + '…') if len(sk) > 8 else '…'


def prefixo_public():
    """Primeiros 8 chars da PUBLIC key. Pública por definição — mostrar
    o prefixo não compromete nada. `pk_test_*` ou `pk_live_*`."""
    pk = (current_app.config.get('PAGARME_PUBLIC_KEY') or '').strip()
    if not pk:
        return ''
    return (pk[:8] + '…') if len(pk) > 8 else '…'


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


def _erro_da_charge(charge):
    """Quando a charge nasce/cai em status 'failed', cava o motivo na
    estrutura aninhada pra propagar mensagem útil em vez de salvar QR vazio.
    Fontes (em ordem): last_transaction.gateway_response.errors[*].message,
    last_transaction.gateway_response.code, last_transaction.acquirer_message,
    charge.status."""
    last = charge.get('last_transaction') or {}
    gw = last.get('gateway_response') or {}
    errs = gw.get('errors') or []
    if errs and isinstance(errs[0], dict):
        msg = errs[0].get('message') or errs[0].get('code')
        if msg:
            return str(msg)
    if gw.get('code'):
        return f"gateway {gw.get('code')}: {gw.get('message') or ''}".strip()
    if last.get('acquirer_message'):
        return str(last.get('acquirer_message'))
    if last.get('gateway_id'):
        return f"gateway_id {last.get('gateway_id')} (status {last.get('status')})"
    return f"charge status={charge.get('status')}"


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
        logger.warning('pagarme criar_pedido_pix HTTP %s: %s', status, body)
        return {'ok': False, 'erro': erro, 'http': status}
    charge = _extrair_charge(body)
    last_tx = charge.get('last_transaction') or {}
    qr = last_tx.get('qr_code')
    # Charge nasceu OK mas falhou na hora (gateway recusou): detecta e
    # propaga o motivo (vai pro PagamentoOnline.erro e pra tela).
    if (charge.get('status') in ('failed', 'canceled')
            or last_tx.get('status') in ('failed', 'canceled')
            or not qr):
        motivo = _erro_da_charge(charge)
        logger.warning('pagarme pix charge falhou: %s | body=%s', motivo, body)
        return {'ok': False, 'erro': motivo, 'http': status,
                'order_id': body.get('id'),
                'charge_id': charge.get('id')}
    return {
        'ok': True,
        'order_id': body.get('id'),
        'charge_id': charge.get('id'),
        'status': charge.get('status'),
        'qr_code': qr,
        'qr_code_url': last_tx.get('qr_code_url'),
        'expira_em': agora() + timedelta(minutes=int(expira_em_min)),
    }


def _billing_address(billing):
    """Normaliza o endereço de cobrança pro formato do Pagar.me v5
    (line_1, zip_code, city, state, country). Exigido pelo antifraude na
    cobrança de cartão."""
    billing = billing or {}
    return {
        'line_1': (billing.get('line_1') or 'S/N')[:255],
        'zip_code': ''.join(c for c in (billing.get('zip_code') or '')
                            if c.isdigit()),
        'city': billing.get('city') or 'São Paulo',
        'state': (billing.get('state') or 'SP')[:2].upper(),
        'country': (billing.get('country') or 'BR')[:2].upper(),
    }


def criar_pedido_cartao(pedido, card_token, parcelas=1, billing=None):
    """Cria Order com payment_method=credit_card usando token tokenizado
    no FRONT (pk_, JS do Pagar.me). O servidor NUNCA vê o número do cartão.
    `billing` = endereço de cobrança (antifraude exige no charge — vai em
    credit_card.card.billing_address, junto com o card_token).
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
                'card': {'billing_address': _billing_address(billing)},
            },
        }],
        'code': pedido.codigo,
    }
    status, body = _post_order(payload)
    if status not in (200, 201):
        erro = body.get('message') or body.get('_erro') or f'HTTP {status}'
        logger.warning('pagarme criar_pedido_cartao HTTP %s: %s', status, body)
        return {'ok': False, 'erro': erro, 'http': status}
    charge = _extrair_charge(body)
    st = charge.get('status')
    last_st = (charge.get('last_transaction') or {}).get('status')
    # Recusado pelo emissor: charge nasce 'failed'/'not_authorized'. Cava o
    # motivo real (mensagem do adquirente) em vez de "recusado" genérico.
    if st in ('failed', 'canceled') or last_st in ('failed', 'not_authorized',
                                                   'refused'):
        motivo = _erro_da_charge(charge)
        logger.warning('pagarme cartao recusado: %s | body=%s', motivo, body)
        return {'ok': False, 'erro': motivo, 'http': status,
                'status': st, 'order_id': body.get('id'),
                'charge_id': charge.get('id')}
    return {
        'ok': True,
        'order_id': body.get('id'),
        'charge_id': charge.get('id'),
        'status': st,  # 'paid', 'pending'…
    }


def qr_data_uri(texto):
    """PNG data-URI de um QR Code do texto (EMV Pix copia-e-cola). Gerado no
    servidor com a lib `qrcode` (já no requirements) pra não depender do
    qr_code_url do Pagar.me (que veio vazio/quebrado no sandbox). Devolve
    None se faltar texto ou a lib falhar."""
    if not texto:
        return None
    try:
        import base64
        import io

        import qrcode
        img = qrcode.make(texto)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return ('data:image/png;base64,'
                + base64.b64encode(buf.getvalue()).decode())
    except Exception:  # noqa: BLE001
        logger.exception('qr_data_uri falhou')
        return None


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


def consultar_order(order_id):
    """Consulta um order no Pagar.me (GET /orders/<id>). Usado pela
    conciliação manual (admin) quando o webhook não chega — a fonte da
    verdade do pagamento é o gateway, não o nosso retorno de checkout.
    Devolve {ok, status, pago, charge_id, charge_status, erro?}."""
    if not disponivel():
        return {'ok': False, 'erro': 'PAGARME_API_KEY não configurada'}
    if not order_id:
        return {'ok': False, 'erro': 'order_id ausente'}
    try:
        r = requests.get(f'{_BASE}/orders/{order_id}', headers=_headers(),
                         timeout=_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        logger.warning('pagarme consultar_order falhou: %s', exc)
        return {'ok': False, 'erro': str(exc)}
    if r.status_code != 200:
        return {'ok': False,
                'erro': f'HTTP {r.status_code}: {(r.text or "")[:200]}'}
    try:
        body = r.json() or {}
    except ValueError:
        return {'ok': False, 'erro': 'resposta sem JSON'}
    status = (body.get('status') or '').lower()
    charge = _extrair_charge(body)
    charge_status = (charge.get('status') or '').lower()
    return {
        'ok': True,
        'status': status,
        'pago': status == 'paid' or charge_status == 'paid',
        'charge_id': charge.get('id'),
        'charge_status': charge_status,
    }

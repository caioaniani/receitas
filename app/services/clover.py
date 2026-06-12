"""Integração com a maquininha Clover (Mini) via REST Pay Display API.

A cobrança é iniciada pelo PDV e a maquininha cuida da captura (cartão
inserido/aproximado, senha, etc). Dois modos de conexão:

- cloud: o servidor fala com a nuvem Clover, que repassa pra maquininha
  rodando o app Cloud Pay Display. Funciona com o sistema hospedado fora
  da loja (Railway). Doc: https://docs.clover.com/dev/docs/rest-pay-intro
- local: o servidor fala direto com a maquininha na rede local (app REST
  Pay Display, porta 12346). Só faz sentido rodando o sistema na loja.

Há ainda o modo 'simulado' (aprova sozinho, pra testar o fluxo do caixa)
e o desativado (CLOVER_MODE vazio — cartão vira captura manual: o operador
digita o valor na maquininha e registra aqui).

Atenção Brasil: a operação Clover BR (Fiserv) processa via SiTef e o
credenciamento de integração é feito com a Fiserv (dvrel@clover.com).
Detalhes e passo a passo: docs/clover-pdv.md
"""
import json
import logging
import time

import requests
from flask import current_app

logger = logging.getLogger(__name__)

# O POST de pagamento só retorna quando o cliente conclui (ou recusa/cancela)
# a operação na maquininha — leitura precisa de timeout longo.
TIMEOUT_PAGAMENTO = (10, 180)
TIMEOUT_CURTO = (10, 20)


def modo():
    return (current_app.config.get('CLOVER_MODE') or '').strip().lower()


def ativo():
    """True se a captura automática na maquininha está habilitada."""
    return modo() in ('cloud', 'local', 'simulado')


def _base_url():
    base = (current_app.config.get('CLOVER_API_BASE') or '').strip().rstrip('/')
    if not base and modo() == 'cloud':
        base = 'https://api.clover.com'
    return base


def _tls_verify():
    # No modo local o certificado vem da CA própria da Clover (não confiada
    # pelo sistema); CLOVER_TLS_VERIFY=0 permite desligar a verificação.
    return (current_app.config.get('CLOVER_TLS_VERIFY') or '1').strip() != '0'


def _headers(idempotency_key=None):
    cfg = current_app.config
    h = {
        'Authorization': f"Bearer {(cfg.get('CLOVER_ACCESS_TOKEN') or '').strip()}",
        'X-Clover-Device-Id': (cfg.get('CLOVER_DEVICE_SERIAL') or '').strip(),
        'X-POS-ID': (cfg.get('CLOVER_POS_ID') or 'OpaoPDV').strip(),
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }
    if idempotency_key:
        h['Idempotency-Key'] = idempotency_key
        h['X-Idempotency-Key'] = idempotency_key  # variação aceita em algumas versões
    return h


def _validar_config():
    if modo() == 'simulado':
        return
    faltando = []
    if not _base_url():
        faltando.append('CLOVER_API_BASE')
    if not (current_app.config.get('CLOVER_ACCESS_TOKEN') or '').strip():
        faltando.append('CLOVER_ACCESS_TOKEN')
    if not (current_app.config.get('CLOVER_DEVICE_SERIAL') or '').strip():
        faltando.append('CLOVER_DEVICE_SERIAL')
    if faltando:
        raise RuntimeError('Clover: configure ' + ', '.join(faltando))


def _mensagem_erro(raw):
    if not isinstance(raw, dict):
        return ''
    err = raw.get('error')
    if isinstance(err, dict):
        return str(err.get('message') or err.get('code') or '')
    return str(raw.get('message') or err or '')


def ping():
    """Verifica se a maquininha está acessível. Retorna {ok, detalhe}."""
    if not ativo():
        return {'ok': False, 'detalhe': 'integração desativada (CLOVER_MODE vazio)'}
    if modo() == 'simulado':
        return {'ok': True, 'detalhe': 'modo simulado'}
    try:
        _validar_config()
    except RuntimeError as e:
        return {'ok': False, 'detalhe': str(e)}
    try:
        r = requests.get(f'{_base_url()}/connect/v1/device/ping',
                         headers=_headers(), timeout=TIMEOUT_CURTO,
                         verify=_tls_verify())
    except requests.RequestException as e:
        return {'ok': False, 'detalhe': f'{type(e).__name__}: {str(e)[:200]}'}
    if r.status_code == 200:
        try:
            body = r.json()
        except ValueError:
            body = {}
        conectado = bool(body.get('connected', True))
        return {'ok': conectado,
                'detalhe': 'maquininha conectada' if conectado else 'maquininha offline'}
    return {'ok': False, 'detalhe': f'HTTP {r.status_code}: {r.text[:200]}'}


def criar_pagamento(valor_centavos, external_id):
    """Envia a cobrança pra maquininha e BLOQUEIA até concluir.

    Sempre chame de uma thread de background (app/blueprints/pdv/caixa.py)
    — a espera pode levar minutos com o cliente na frente da maquininha.

    Retorna {aprovado: bool, payment_id, mensagem, raw}.
    Levanta RuntimeError em erro de configuração/comunicação.
    """
    valor_centavos = int(valor_centavos)
    if valor_centavos <= 0:
        raise RuntimeError('Clover: valor inválido')
    if modo() == 'simulado':
        time.sleep(4)  # simula o cliente passando o cartão
        return {'aprovado': True, 'payment_id': f'SIM-{external_id}',
                'mensagem': 'aprovado (simulado)', 'raw': {'simulado': True}}
    _validar_config()
    body = {
        'amount': valor_centavos,
        'externalPaymentId': external_id,
        'final': True,
        'capture': True,
    }
    try:
        r = requests.post(f'{_base_url()}/connect/v1/payments',
                          headers=_headers(idempotency_key=external_id),
                          json=body, timeout=TIMEOUT_PAGAMENTO,
                          verify=_tls_verify())
    except requests.RequestException as e:
        raise RuntimeError(f'falha de comunicação com a Clover: '
                           f'{type(e).__name__}: {str(e)[:200]}')
    try:
        raw = r.json()
    except ValueError:
        raw = {'texto': r.text[:500]}
    if r.status_code not in (200, 201):
        logger.error('Clover pagamento HTTP %s: %s', r.status_code, r.text[:300])
        return {'aprovado': False, 'payment_id': None,
                'mensagem': f'HTTP {r.status_code}: {_mensagem_erro(raw) or r.text[:200]}',
                'raw': raw}
    pay = raw.get('payment') if isinstance(raw.get('payment'), dict) else raw
    resultado = str(pay.get('result') or raw.get('result') or '').upper()
    aprovado = resultado in ('SUCCESS', 'APPROVED', 'AUTH')
    return {'aprovado': aprovado,
            'payment_id': pay.get('id') or raw.get('paymentId'),
            'mensagem': resultado or _mensagem_erro(raw) or 'sem resultado',
            'raw': raw}


def cancelar_operacao():
    """Cancela a operação em andamento na tela da maquininha."""
    if modo() == 'simulado':
        return {'ok': True, 'detalhe': 'modo simulado'}
    try:
        _validar_config()
    except RuntimeError as e:
        return {'ok': False, 'detalhe': str(e)}
    try:
        r = requests.post(f'{_base_url()}/connect/v1/device/cancel',
                          headers=_headers(), json={}, timeout=TIMEOUT_CURTO,
                          verify=_tls_verify())
        return {'ok': r.status_code in (200, 204), 'detalhe': r.text[:200]}
    except requests.RequestException as e:
        return {'ok': False, 'detalhe': f'{type(e).__name__}: {str(e)[:200]}'}


def resposta_json(raw):
    """Serializa a resposta crua pra guardar em VendaPagamento.clover_resposta."""
    try:
        return json.dumps(raw, ensure_ascii=False)[:4000]
    except (TypeError, ValueError):
        return str(raw)[:4000]

"""Lalamove (entrega sob demanda) — API v3 (Aries).

Fluxo: o atendente marca PRONTO no painel do dia -> cota (moto/carro) ->
chama o entregador pro endereço do pedido. Origem fixa: filial Anésio
Pinto Rosa (decisão do dono, 10/06/2026).

Autenticação v3: HMAC-SHA256 sobre
    "{timestamp_ms}\r\n{METODO}\r\n{path}\r\n\r\n{body_json}"
com header `Authorization: hmac {KEY}:{ts}:{assinatura}` + `Market: BR`.

Env (Railway): LALAMOVE_API_KEY, LALAMOVE_API_SECRET, LALAMOVE_MARKET
(default BR), LALAMOVE_BASE_URL (default produção; sandbox =
https://rest.sandbox.lalamove.com), LALAMOVE_ORIGEM_ENDERECO,
LALAMOVE_ORIGEM_LATLNG ("lat,lng" — pula o geocoder), LALAMOVE_REMETENTE_NOME,
LALAMOVE_REMETENTE_FONE.

Qualquer recusa da API (4xx/5xx) volta como {'ok': False, 'erro': ...} com a
mensagem deles — nunca silenciada (aparece no modal do atendente).
"""
import hashlib
import hmac
import json
import logging
import re
import time

import requests
from flask import current_app

logger = logging.getLogger(__name__)

_TIMEOUT = 15

ORIGEM_ENDERECO_DEFAULT = ('Rua Anésio Pinto Rosa, 78 - Brooklin Paulista, '
                           'São Paulo - SP, 04570-130')

# Tipos de veículo do mercado BR (lista oficial confirmada pelo 422 da
# propria API em prod, 10/06/2026, BR SAO). Ordem = mais usados primeiro;
# o atendente escolhe no seletor do painel. Os "4h/6h" sao aluguel por
# periodo (varias paradas), raros pra entrega de pedido.
OPCOES_VEICULO = [
    ('LALAGO', '🏍 Moto'),
    ('LALAPRO', '🏍 Moto Pro (baú maior)'),
    ('CAR', '🚗 Carro'),
    ('HATCHBACK', '🚗 Hatch'),
    ('UV_FIORINO', '🛻 Fiorino'),
    ('VAN', '🚐 Van'),
    ('TRUCK330', '🚚 Caminhão pequeno (330kg)'),
    ('TRUCK3_5T', '🚚 Caminhão 3,5t'),
    ('LALAGOFOUR', '🏍 Moto — aluguel 4h'),
    ('CARFOURH', '🚗 Carro — aluguel 4h'),
    ('HATCHFOURH', '🚗 Hatch — aluguel 4h'),
    ('VANFOURH', '🚐 Van — aluguel 4h'),
    ('UV_4H', '🛻 Utilitário — aluguel 4h'),
    ('TRUCK_6H', '🚚 Caminhão — aluguel 6h'),
]
ROTULO_VEICULO = dict(OPCOES_VEICULO)

# Apelidos usados por chamadas antigas/bot — mapeiam pros nomes da API.
SERVICE_TYPES = {'moto': 'LALAGO', 'carro': 'CAR'}

STATUS_LABEL = {
    'ASSIGNING_DRIVER': 'Procurando entregador',
    'ON_GOING': 'Entregador a caminho da padaria',
    'PICKED_UP': 'Saiu para entrega',
    'COMPLETED': 'Entregue',
    'CANCELED': 'Cancelada',
    'REJECTED': 'Recusada pela Lalamove',
    'EXPIRED': 'Expirou sem entregador',
}

# Cache por worker das coordenadas da origem (geocodifica 1x).
_origem_cache = None


def _cfg(nome, default=''):
    return (current_app.config.get(nome)
            or __import__('os').environ.get(nome, default))


def disponivel():
    return bool(_cfg('LALAMOVE_API_KEY') and _cfg('LALAMOVE_API_SECRET'))


def _base_url():
    return (_cfg('LALAMOVE_BASE_URL') or 'https://rest.lalamove.com').rstrip('/')


def _request(metodo, path, payload=None):
    """Chamada assinada à API v3. Retorna (status_code, dict_da_resposta)."""
    key = _cfg('LALAMOVE_API_KEY')
    secret = _cfg('LALAMOVE_API_SECRET')
    body = json.dumps(payload, ensure_ascii=False) if payload is not None else ''
    ts = str(int(time.time() * 1000))
    raw = f'{ts}\r\n{metodo.upper()}\r\n{path}\r\n\r\n{body}'
    assinatura = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    headers = {
        'Authorization': f'hmac {key}:{ts}:{assinatura}',
        'Market': _cfg('LALAMOVE_MARKET', 'BR') or 'BR',
        'Content-Type': 'application/json',
    }
    r = requests.request(metodo.upper(), _base_url() + path,
                         data=body.encode() if body else None,
                         headers=headers, timeout=_TIMEOUT)
    try:
        corpo = r.json() if r.text else {}
    except ValueError:
        corpo = {'raw': r.text[:500]}
    return r.status_code, corpo


def _erro_api(status, corpo, contexto):
    """Mensagem legível pro atendente a partir do erro da Lalamove."""
    erros = corpo.get('errors') or []
    detalhe = '; '.join(f"{e.get('id') or e.get('code', '?')}: "
                        f"{e.get('message') or e.get('detail', '')}"
                        for e in erros if isinstance(e, dict)) or str(corpo)[:300]
    logger.warning('lalamove %s falhou (%s): %s', contexto, status, detalhe)
    return {'ok': False, 'erro': f'Lalamove recusou ({status}): {detalhe}'}


def _fone_e164(fone):
    """Telefone BR pro formato +55DDDNUMERO exigido pela API."""
    dig = re.sub(r'\D', '', fone or '')
    if not dig:
        return None
    if dig.startswith('55') and len(dig) >= 12:
        return f'+{dig}'
    if len(dig) in (10, 11):
        return f'+55{dig}'
    return f'+{dig}'


def _origem():
    """(lat, lng, endereco) da filial de saída. Env LALAMOVE_ORIGEM_LATLNG
    fixa as coordenadas; sem ela, valida rua/número no Google uma vez por
    worker. Nunca usa centroide de CEP para enviar o motorista."""
    global _origem_cache
    if _origem_cache:
        return _origem_cache
    endereco = _cfg('LALAMOVE_ORIGEM_ENDERECO') or ORIGEM_ENDERECO_DEFAULT
    fixo = (_cfg('LALAMOVE_ORIGEM_LATLNG') or '').strip()
    if fixo and ',' in fixo:
        try:
            lat, lng = (float(x) for x in fixo.split(',', 1))
            _origem_cache = (lat, lng, endereco)
            return _origem_cache
        except ValueError:
            logger.warning('LALAMOVE_ORIGEM_LATLNG inválido: %r', fixo)
    from app.services import frete
    geo = frete.geocodificar_entrega(endereco)
    if not geo:
        return None
    _origem_cache = (geo[0], geo[1], endereco)
    return _origem_cache


def cotar(endereco_destino, tipo_veiculo):
    """Cota uma entrega da filial até o endereço. tipo_veiculo: moto|carro.

    Sucesso: {'ok': True, 'quotation_id', 'valor' (str), 'moeda',
    'distancia_m' (int|None), 'sender_stop_id', 'recipient_stop_id',
    'service_type', 'expira_em'}.
    """
    if not disponivel():
        return {'ok': False, 'erro': 'Lalamove sem credenciais configuradas'}
    # Aceita apelido (moto/carro) ou o nome direto da API (LALAGO, VAN...).
    bruto = (tipo_veiculo or '').strip()
    st = SERVICE_TYPES.get(bruto.lower()) \
        or (bruto.upper() if bruto.upper() in ROTULO_VEICULO else None)
    if not st:
        return {'ok': False, 'erro': f'veículo inválido: {tipo_veiculo}'}
    origem = _origem()
    if not origem:
        return {'ok': False, 'erro': 'não consegui localizar o endereço de '
                                     'origem (geocodificação falhou)'}
    from app.services import frete
    destino = frete.geocodificar_entrega(endereco_destino)
    if not destino:
        # Sem coordenada não dá pra cotar/despachar a corrida — o dono precisa
        # saber (motoboy não sai): sensor no painel + WhatsApp na hora
        # (decisão do dono 09/07). Best-effort, nunca quebra a cotação.
        from app.services import frete_sensor, loja_alerta
        frete_sensor.registrar('lalamove', 'lalamove_falhou',
                               endereco=endereco_destino)
        loja_alerta.alertar_endereco_falho(endereco_destino, motivo='lalamove')
        return {'ok': False, 'erro': 'Não foi possível confirmar o ponto de entrega. '
                                     'Confira rua, número e CEP ou informe as '
                                     'coordenadas do local. A corrida não foi chamada.'}
    olat, olng, oend = origem
    payload = {'data': {
        'serviceType': st,
        'language': 'pt_BR',
        'stops': [
            {'coordinates': {'lat': f'{olat:.6f}', 'lng': f'{olng:.6f}'},
             'address': oend},
            {'coordinates': {'lat': f'{destino[0]:.6f}', 'lng': f'{destino[1]:.6f}'},
             'address': endereco_destino},
        ],
    }}
    status, corpo = _request('POST', '/v3/quotations', payload)
    if status not in (200, 201):
        return _erro_api(status, corpo, 'cotação')
    d = corpo.get('data') or {}
    stops = d.get('stops') or []
    preco = d.get('priceBreakdown') or {}
    dist = d.get('distance') or {}
    try:
        distancia_m = int(float(dist.get('value')))
    except (TypeError, ValueError):
        distancia_m = None
    return {
        'ok': True,
        'quotation_id': d.get('quotationId'),
        'valor': preco.get('total'),
        'moeda': preco.get('currency') or 'BRL',
        'distancia_m': distancia_m,
        'sender_stop_id': (stops[0].get('stopId') if stops else None),
        'recipient_stop_id': (stops[1].get('stopId') if len(stops) > 1 else None),
        'service_type': st,
        'expira_em': d.get('expiresAt'),
    }


def criar_ordem(quotation_id, sender_stop_id, recipient_stop_id,
                destinatario, telefone_destino, observacao=None):
    """Confirma a corrida em cima de uma cotação. Sucesso:
    {'ok': True, 'order_id', 'status', 'share_link', 'valor', 'moeda'}."""
    if not disponivel():
        return {'ok': False, 'erro': 'Lalamove sem credenciais configuradas'}
    remetente_nome = _cfg('LALAMOVE_REMETENTE_NOME') or 'O Pão Padaria Artesanal'
    remetente_fone = _fone_e164(_cfg('LALAMOVE_REMETENTE_FONE')
                                or _cfg('ZAPI_BOT_DONO_NUMERO'))
    fone_dest = _fone_e164(telefone_destino) or remetente_fone
    if not remetente_fone:
        return {'ok': False, 'erro': 'configure LALAMOVE_REMETENTE_FONE no '
                                     'Railway (telefone da filial)'}
    payload = {'data': {
        'quotationId': quotation_id,
        'sender': {'stopId': sender_stop_id, 'name': remetente_nome,
                   'phone': remetente_fone},
        'recipients': [{'stopId': recipient_stop_id,
                        'name': (destinatario or 'Cliente')[:64],
                        'phone': fone_dest,
                        'remarks': (observacao or '')[:500]}],
        'isPODEnabled': False,
    }}
    status, corpo = _request('POST', '/v3/orders', payload)
    if status not in (200, 201):
        return _erro_api(status, corpo, 'criação de ordem')
    d = corpo.get('data') or {}
    preco = d.get('priceBreakdown') or {}
    return {'ok': True, 'order_id': d.get('orderId'),
            'status': d.get('status') or 'ASSIGNING_DRIVER',
            'share_link': d.get('shareLink'),
            'valor': preco.get('total'), 'moeda': preco.get('currency')}


def adicionar_priority_fee(order_id, valor):
    """Adiciona/atualiza a gorjeta (priority fee) de uma corrida pra acelerar
    a alocacao do entregador.

    Regras da Lalamove (v3):
    - Só vale ENQUANTO procura entregador (antes de o motorista aceitar).
    - Cada novo valor SUBSTITUI o anterior e precisa ser MAIOR que ele.
    - Endpoint POST /v3/orders/{id}/priority-fee.

    `valor`: número (reais). Vira string com 2 casas no payload.

    Sucesso: {'ok': True, 'priority_fee' (str), 'total' (str|None),
    'moeda'}. A doc deles está fechada (403); o campo do body é inferido
    como `priorityFee` (mesmo nome do priceBreakdown). Se a API recusar por
    nome de campo, o erro cru aparece pro atendente via `_erro_api`.
    """
    if not disponivel():
        return {'ok': False, 'erro': 'Lalamove sem credenciais configuradas'}
    if not order_id:
        return {'ok': False, 'erro': 'corrida sem order_id'}
    try:
        v = float(str(valor).replace(',', '.'))
    except (TypeError, ValueError):
        return {'ok': False, 'erro': f'valor inválido: {valor}'}
    if v <= 0:
        return {'ok': False, 'erro': 'a gorjeta precisa ser maior que zero'}
    payload = {'data': {'priorityFee': f'{v:.2f}'}}
    status, corpo = _request('POST', f'/v3/orders/{order_id}/priority-fee',
                             payload)
    if status not in (200, 201):
        return _erro_api(status, corpo, 'priority fee')
    d = corpo.get('data') or {}
    preco = d.get('priceBreakdown') or {}
    return {
        'ok': True,
        'priority_fee': preco.get('priorityFee') or f'{v:.2f}',
        'total': preco.get('total'),
        'moeda': preco.get('currency') or 'BRL',
    }


def detalhes(order_id):
    status, corpo = _request('GET', f'/v3/orders/{order_id}')
    if status != 200:
        return _erro_api(status, corpo, 'consulta de ordem')
    d = corpo.get('data') or {}
    return {'ok': True, 'status': d.get('status'),
            'share_link': d.get('shareLink'), 'driver_id': d.get('driverId')}


def cancelar(order_id):
    status, corpo = _request('DELETE', f'/v3/orders/{order_id}')
    if status not in (200, 204):
        return _erro_api(status, corpo, 'cancelamento')
    return {'ok': True}


def rotulo_status(status):
    return STATUS_LABEL.get((status or '').upper(), status or '?')

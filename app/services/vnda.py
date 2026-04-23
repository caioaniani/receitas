"""Servico de integracao com a API Vnda (e-commerce)."""

import logging
from datetime import datetime, date, timedelta

import requests
from flask import current_app

logger = logging.getLogger(__name__)

_client_cache = {}


def _headers():
    token = current_app.config.get('VNDA_API_TOKEN', '')
    host = current_app.config.get('VNDA_SHOP_HOST', 'www.padariaartesanalonline.com.br')
    return {
        'Authorization': f'Bearer {token}',
        'X-Shop-Host': host,
        'Accept': 'application/json',
    }


def _base_url():
    host = current_app.config.get('VNDA_SHOP_HOST', 'www.padariaartesanalonline.com.br')
    return f'https://{host}/api/v2'


def _get(endpoint, params=None):
    url = f'{_base_url()}{endpoint}'
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.error('Erro API Vnda %s: %s', endpoint, e)
        return None


def _extrair_data_entrega(order):
    edd = order.get('expected_delivery_date')
    if edd:
        try:
            return datetime.fromisoformat(edd.replace('Z', '+00:00')).date()
        except (ValueError, TypeError):
            pass
    extra = order.get('extra') or {}
    data_br = extra.get('DataDeEntrega', '')
    if data_br:
        try:
            return datetime.strptime(data_br.strip(), '%d/%m/%Y').date()
        except (ValueError, TypeError):
            pass
    return None


def _extrair_periodo(order):
    extra = order.get('extra') or {}
    return extra.get('Periodo', '')


def _normalizar_pedido(order, client_data=None):
    data_entrega = _extrair_data_entrega(order)

    itens = []
    for item in (order.get('items') or []):
        itens.append({
            'sku': item.get('sku', ''),
            'nome': item.get('product_name', item.get('name', '')),
            'quantidade': item.get('quantity', 1),
            'preco_unitario': item.get('price', 0),
            'subtotal': item.get('subtotal', 0),
        })

    nome_cliente = ''
    telefone = ''
    endereco = ''

    if client_data:
        nome_cliente = client_data.get('name', '')
        telefone = client_data.get('phone', '')
        addr = client_data.get('recent_address') or {}
        if addr:
            parts = [
                addr.get('street', ''), addr.get('number', ''),
                addr.get('complement', ''), addr.get('neighborhood', ''),
                addr.get('city', ''), addr.get('state', ''),
            ]
            endereco = ', '.join(p for p in parts if p)

    if not nome_cliente:
        nome_cliente = order.get('client_name', '')
    if not endereco:
        shipping = order.get('shipping_address') or {}
        if shipping:
            parts = [
                shipping.get('street', ''), shipping.get('number', ''),
                shipping.get('complement', ''), shipping.get('neighborhood', ''),
                shipping.get('city', ''), shipping.get('state', ''),
            ]
            endereco = ', '.join(p for p in parts if p)

    return {
        'code': order.get('code', ''),
        'status_vnda': order.get('status', ''),
        'data_entrega': data_entrega.isoformat() if data_entrega else None,
        'data_entrega_fmt': data_entrega.strftime('%d/%m/%Y') if data_entrega else '',
        'periodo': _extrair_periodo(order),
        'cliente_nome': nome_cliente,
        'cliente_telefone': telefone,
        'endereco': endereco,
        'itens': itens,
        'total': order.get('total', 0),
        'tem_customizacao': bool(order.get('customizations')),
        'observacao': order.get('note', ''),
    }


def buscar_cliente(client_id):
    if not client_id:
        return None
    if client_id in _client_cache:
        return _client_cache[client_id]
    data = _get(f'/clients/{client_id}')
    if data:
        _client_cache[client_id] = data
    return data


def _buscar_pedidos_janela(start_date, end_date):
    """Busca todos os pedidos numa janela de datas (com paginacao)."""
    todos = []
    page = 1
    per_page = 50
    while True:
        params = {
            'per_page': per_page,
            'page': page,
            'start': start_date.isoformat(),
            'finish': end_date.isoformat(),
        }
        data = _get('/orders', params=params)
        if not data:
            break
        if isinstance(data, list):
            todos.extend(data)
            if len(data) < per_page:
                break
        elif isinstance(data, dict) and 'results' in data:
            todos.extend(data['results'])
            if len(data['results']) < per_page:
                break
        else:
            break
        page += 1
        if page > 20:
            break
    return todos


def buscar_pedidos_do_dia(target_date):
    token = current_app.config.get('VNDA_API_TOKEN')
    if not token:
        return {'erro': 'Token Vnda nao configurado. Adicione VNDA_API_TOKEN nas variaveis de ambiente.', 'pedidos': []}

    start = target_date - timedelta(days=30)
    end = target_date + timedelta(days=5)
    todos = _buscar_pedidos_janela(start, end)

    logger.info('Vnda: %d pedidos na janela, filtrando para %s', len(todos), target_date)

    pedidos = []
    for order in todos:
        de = _extrair_data_entrega(order)
        if de != target_date:
            continue
        client_id = order.get('client_id')
        client_data = buscar_cliente(client_id)
        pedidos.append(_normalizar_pedido(order, client_data))

    pedidos.sort(key=lambda p: p.get('periodo') or '')
    return {'pedidos': pedidos, 'total_janela': len(todos)}


def contar_pedidos_por_dia(year, month):
    if not current_app.config.get('VNDA_API_TOKEN'):
        return {}

    first = date(year, month, 1)
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)

    start = first - timedelta(days=30)
    end = last + timedelta(days=5)
    todos = _buscar_pedidos_janela(start, end)

    contagem = {}
    for order in todos:
        de = _extrair_data_entrega(order)
        if de and first <= de <= last:
            key = de.isoformat()
            contagem[key] = contagem.get(key, 0) + 1

    return contagem

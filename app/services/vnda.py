"""Servico de integracao com a API Vnda (e-commerce)."""

import logging
from datetime import datetime, date, timedelta

import requests
from flask import current_app

logger = logging.getLogger(__name__)

_client_cache = {}


def _headers():
    token = current_app.config.get('VNDA_API_TOKEN', '')
    if token.lower().startswith('bearer '):
        token = token[7:]
    host = current_app.config.get('VNDA_SHOP_HOST', 'www.padariaartesanalonline.com.br')
    return {
        'Authorization': f'Bearer {token}',
        'X-Shop-Host': host,
        'Accept': 'application/json',
        'User-Agent': 'OPaoPadaria/1.0',
    }


def _base_url():
    return 'https://api.vnda.com.br/api/v2'


def _get(endpoint, params=None):
    url = f'{_base_url()}{endpoint}'
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=15)
        resp.raise_for_status()
        return resp
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


def _extrair_endereco(addr):
    if not addr:
        return '', '', ''
    nome = ' '.join(p for p in [addr.get('first_name', ''), addr.get('last_name', '')] if p)
    tel = addr.get('phone', '')
    parts = [
        addr.get('street', ''), addr.get('number', ''),
        addr.get('complement', ''), addr.get('neighborhood', ''),
        addr.get('city', ''), addr.get('state', ''),
    ]
    zip_code = addr.get('zip_code', addr.get('zip', ''))
    if zip_code:
        parts.append(zip_code)
    end = ', '.join(p for p in parts if p)
    return nome, tel, end


def buscar_pedido_completo(code):
    """Busca detalhes completos de um pedido (inclui shipping_address)."""
    resp = _get(f'/orders/{code}')
    if resp:
        try:
            return resp.json()
        except ValueError:
            pass
    return None


def _normalizar_pedido(order, client_data=None, order_detail=None):
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

    comprador = ''
    destinatario = ''
    telefone_dest = ''
    endereco_entrega = ''

    if client_data:
        comprador = client_data.get('name', '')

    if not comprador:
        comprador = order.get('client_name', '')

    detail = order_detail or order
    shipping = detail.get('shipping_address') or {}
    if shipping:
        destinatario, telefone_dest, endereco_entrega = _extrair_endereco(shipping)

    if not endereco_entrega and client_data:
        addr = client_data.get('recent_address') or {}
        if addr:
            dest_name, dest_tel, endereco_entrega = _extrair_endereco(addr)
            if not destinatario:
                destinatario = dest_name
            if not telefone_dest:
                telefone_dest = dest_tel

    if not destinatario:
        destinatario = comprador

    return {
        'code': order.get('code', ''),
        'status_vnda': order.get('status', ''),
        'data_entrega': data_entrega.isoformat() if data_entrega else None,
        'data_entrega_fmt': data_entrega.strftime('%d/%m/%Y') if data_entrega else '',
        'periodo': _extrair_periodo(order),
        'comprador': comprador,
        'destinatario': destinatario,
        'telefone': telefone_dest,
        'endereco': endereco_entrega,
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
    resp = _get(f'/clients/{client_id}')
    if resp:
        try:
            data = resp.json()
            _client_cache[client_id] = data
            return data
        except ValueError:
            pass
    return None


def _buscar_pedidos_janela(start_date, end_date):
    """Busca todos os pedidos numa janela de datas (com paginacao)."""
    todos = []
    page = 1
    per_page = 100
    while True:
        params = {
            'per_page': per_page,
            'page': page,
            'start': start_date.isoformat(),
            'finish': end_date.isoformat(),
        }
        resp = _get('/orders', params=params)
        if not resp:
            break
        try:
            data = resp.json()
        except ValueError:
            break

        if isinstance(data, list):
            todos.extend(data)
            if len(data) < per_page:
                break
        else:
            break

        pagination = resp.headers.get('X-Pagination', '')
        if pagination:
            import json as _json
            try:
                pag = _json.loads(pagination)
                if not pag.get('next_page'):
                    break
            except ValueError:
                pass

        page += 1
        if page > 50:
            break
    return todos


_STATUS_IGNORAR = {'canceled', 'cancelled'}


def buscar_pedidos_do_dia(target_date):
    token = current_app.config.get('VNDA_API_TOKEN')
    if not token:
        return {'erro': 'Token Vnda nao configurado. Adicione VNDA_API_TOKEN nas variaveis de ambiente.', 'pedidos': []}

    start = target_date - timedelta(days=14)
    end = target_date + timedelta(days=3)
    todos = _buscar_pedidos_janela(start, end)

    logger.info('Vnda: %d pedidos na janela, filtrando para %s', len(todos), target_date)

    pedidos = []
    for order in todos:
        if (order.get('status') or '').lower() in _STATUS_IGNORAR:
            continue
        de = _extrair_data_entrega(order)
        if de != target_date:
            continue
        client_id = order.get('client_id')
        client_data = buscar_cliente(client_id)
        order_detail = buscar_pedido_completo(order.get('code'))
        pedidos.append(_normalizar_pedido(order, client_data, order_detail))

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
        if (order.get('status') or '').lower() in _STATUS_IGNORAR:
            continue
        de = _extrair_data_entrega(order)
        if de and first <= de <= last:
            key = de.isoformat()
            contagem[key] = contagem.get(key, 0) + 1

    return contagem

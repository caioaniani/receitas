"""Servico de integracao com a API Vnda (e-commerce)."""

import logging
import re
from datetime import datetime, date, timedelta

import requests
from flask import current_app

logger = logging.getLogger(__name__)

_client_cache = {}

# Cache curto de buscar_pedidos_do_dia: chave = (data_iso, overrides_hash) → (timestamp, resultado)
# Reduz dependencia da API VNDA, que as vezes retorna lento ou vazio temporariamente.
_pedidos_cache = {}
_PEDIDOS_CACHE_TTL = 60  # segundos


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


def _is_entrega_expressa(order):
    """Detecta pedidos com frete expresso (entrega em 1 hora)."""
    candidatos = [
        order.get('delivery_type', ''),
        order.get('shipping_label', ''),
        order.get('shipping_method_code', ''),
        order.get('shipping_method', ''),
        order.get('shipping_name', ''),
        order.get('shipping_method_name', ''),
    ]
    extra = order.get('extra') or {}
    for v in extra.values():
        if isinstance(v, str):
            candidatos.append(v)
    for v in candidatos:
        if isinstance(v, str) and 'hora' in v.lower():
            return True
    return False


def _parse_iso_date(val):
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace('Z', '+00:00')).date()
    except (ValueError, TypeError):
        return None


def _segundo_domingo(year, month):
    """Retorna o 2o domingo do mes (usado para Dia das Maes/Pais)."""
    primeiro = date(year, month, 1)
    delta = (6 - primeiro.weekday()) % 7  # weekday: 0=seg, 6=dom
    return date(year, month, 1 + delta + 7)


def _extrair_data_de_label(label, ref_year=None):
    """Tenta extrair data de uma string livre de shipping_label.
    Reconhece DD/MM/YYYY, DD/MM e keywords de eventos comemorativos
    (Dia das Maes, Natal, etc)."""
    if not label:
        return None
    label_lower = label.lower()
    if not ref_year:
        ref_year = date.today().year

    # 1. DD/MM/YYYY explicito
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', label)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass

    # 2. DD/MM (sem ano) — assume ref_year
    m = re.search(r'(?:^|[^\d/])(\d{1,2})/(\d{1,2})(?![/\d])', label)
    if m:
        try:
            return date(ref_year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass

    # 3. Keywords de feriados
    if 'mãe' in label_lower or 'maes' in label_lower or 'mae' in label_lower:
        return _segundo_domingo(ref_year, 5)
    if 'pais' in label_lower:
        return _segundo_domingo(ref_year, 8)
    if 'natal' in label_lower:
        return date(ref_year, 12, 25)
    if 'namorad' in label_lower:
        return date(ref_year, 6, 12)

    return None


def _extrair_data_entrega(order):
    # 1. extra.DataDeEntrega — campo customizado do checkout (formato confiavel)
    extra = order.get('extra') or {}
    data_br = extra.get('DataDeEntrega', '')
    if data_br:
        try:
            return datetime.strptime(data_br.strip(), '%d/%m/%Y').date()
        except (ValueError, TypeError):
            pass

    # 2. shipping_label com data explicita ou keyword de feriado
    # (formato novo de checkout que nao preenche extra.DataDeEntrega)
    ref = _parse_iso_date(order.get('confirmed_at')) or _parse_iso_date(order.get('paid_at')) or date.today()
    label_data = _extrair_data_de_label(order.get('shipping_label', ''), ref.year)
    if label_data:
        return label_data

    # 3. Entrega expressa (1h): usa data de confirmacao
    if _is_entrega_expressa(order):
        for campo in ('received_at', 'confirmed_at', 'paid_at', 'created_at'):
            d = _parse_iso_date(order.get(campo))
            if d:
                return d

    # 4. expected_delivery_date — VNDA calcula automatico, frequentemente
    # incorreto para encomendas agendadas (cai na data de confirmacao)
    edd = order.get('expected_delivery_date')
    if edd:
        try:
            return datetime.fromisoformat(edd.replace('Z', '+00:00')).date()
        except (ValueError, TypeError):
            pass
    return None


def _extrair_periodo(order):
    extra = order.get('extra') or {}
    periodo = extra.get('Periodo', '')
    if periodo:
        return periodo
    if _is_entrega_expressa(order):
        return 'Expresso (1h)'
    return ''


def _extrair_endereco(addr):
    if not addr:
        return '', '', ''
    # recipient_name = nome do destinatario real (pode ser != comprador).
    # Disponivel em /orders/{code}/shipping_address. Tem prioridade.
    nome = (addr.get('recipient_name') or '').strip()
    if not nome:
        nome = ' '.join(p for p in [addr.get('first_name', ''), addr.get('last_name', '')] if p)
    tel = addr.get('phone', '')
    if not tel:
        area = addr.get('first_phone_area', '')
        phone = addr.get('first_phone', '')
        if area or phone:
            tel = f'({area}) {phone}' if area else phone
    # Aceita ambos os schemas: street/number (cliente recent) e street_name/street_number (shipping_address)
    parts = [
        addr.get('street') or addr.get('street_name', ''),
        addr.get('number') or addr.get('street_number', ''),
        addr.get('complement', ''),
        addr.get('neighborhood', ''),
        addr.get('city', ''),
        addr.get('state', ''),
    ]
    zip_code = addr.get('zip_code', addr.get('zip', ''))
    if zip_code:
        parts.append(zip_code)
    end = ', '.join(p for p in parts if p)
    return nome, tel, end


def buscar_shipping_address(code):
    """Busca o endereco de entrega de um pedido (endpoint dedicado).
    Diferente de /orders/{code}, este endpoint inclui recipient_name —
    o nome real do destinatario, que pode ser diferente do comprador.
    """
    resp = _get(f'/orders/{code}/shipping_address')
    if resp:
        try:
            return resp.json()
        except ValueError:
            pass
    return None


def buscar_customizations(code, item_id):
    """Customizacoes (cartinha, etc) de um item especifico do pedido."""
    resp = _get(f'/orders/{code}/items/{item_id}/customizations')
    if resp:
        try:
            data = resp.json()
            if isinstance(data, list):
                return data
        except ValueError:
            pass
    return []


def _extrair_cartinha(items_customizations):
    """Concatena texto de customizations cujo group_name indica cartinha/recado."""
    if not items_customizations:
        return ''
    keywords = ('cartinha', 'mensagem', 'recado', 'card')
    textos = []
    for item_customs in items_customizations:
        for c in (item_customs or []):
            group = (c.get('group_name') or '').strip().lower()
            if any(k in group for k in keywords):
                texto = (c.get('name') or '').strip()
                if texto:
                    textos.append(texto)
    return ' · '.join(textos)


def buscar_pedido_completo(code):
    """Busca detalhes completos de um pedido (inclui shipping_address)."""
    resp = _get(f'/orders/{code}')
    if resp:
        try:
            return resp.json()
        except ValueError:
            pass
    return None


def _normalizar_pedido(order, client_data=None, shipping_data=None, items_customizations=None):
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

    cartinha_vnda = _extrair_cartinha(items_customizations or [])
    tem_customizacao = bool(cartinha_vnda) or any(
        item.get('has_customizations') for item in (order.get('items') or [])
    )

    comprador = ''
    destinatario = ''
    telefone_dest = ''
    endereco_entrega = ''

    if client_data:
        comprador = client_data.get('name', '')

    if not comprador:
        comprador = order.get('client_name', '')

    # Prioridade 1: /orders/{code}/shipping_address (recipient_name + endereco real)
    if shipping_data:
        destinatario, telefone_dest, endereco_entrega = _extrair_endereco(shipping_data)

    # Prioridade 2: shipping_address embutido no order (formato antigo)
    if not destinatario or not endereco_entrega:
        shipping_inline = order.get('shipping_address') or {}
        if shipping_inline:
            d, t, e = _extrair_endereco(shipping_inline)
            destinatario = destinatario or d
            telefone_dest = telefone_dest or t
            endereco_entrega = endereco_entrega or e

    # Prioridade 3: recent_address do cliente (fallback final, pode trazer dados de pedido anterior)
    if not endereco_entrega and client_data:
        addr = client_data.get('recent_address') or {}
        if addr:
            d, t, e = _extrair_endereco(addr)
            destinatario = destinatario or d
            telefone_dest = telefone_dest or t
            endereco_entrega = e

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
        'tem_customizacao': tem_customizacao,
        'cartinha_vnda': cartinha_vnda,
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


def buscar_pedidos_do_dia(target_date, overrides=None):
    """overrides: dict {pedido_code: data_entrega} para sobrescrever a data extraida."""
    import time
    token = current_app.config.get('VNDA_API_TOKEN')
    if not token:
        return {'erro': 'Token Vnda nao configurado. Adicione VNDA_API_TOKEN nas variaveis de ambiente.', 'pedidos': []}

    overrides = overrides or {}

    # Cache curto de 60s. Mesma data + mesmos overrides → reaproveita.
    cache_key = (target_date.isoformat(),
                 tuple(sorted((k, v.isoformat() if hasattr(v, 'isoformat') else v) for k, v in overrides.items())))
    cached = _pedidos_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _PEDIDOS_CACHE_TTL:
        return cached[1]

    # Janela de criacao: pedidos VNDA podem ser feitos com bastante antecedencia
    # (encomendas de bolos, datas comemorativas). 60 dias cobre o caso geral.
    start = target_date - timedelta(days=60)
    end = target_date + timedelta(days=3)
    todos = _buscar_pedidos_janela(start, end)

    logger.info('Vnda: %d pedidos na janela, filtrando para %s', len(todos), target_date)

    pedidos = []
    for order in todos:
        if (order.get('status') or '').lower() in _STATUS_IGNORAR:
            continue
        code = order.get('code')
        de_original = _extrair_data_entrega(order)
        de = overrides.get(code) or de_original
        if de != target_date:
            continue
        client_id = order.get('client_id')
        client_data = buscar_cliente(client_id)
        shipping_data = buscar_shipping_address(code)
        # Busca customizations dos items que tem (otimizacao: skip items sem customizacao)
        items_customs = []
        for item in (order.get('items') or []):
            if item.get('has_customizations') and item.get('id'):
                items_customs.append(buscar_customizations(code, item['id']))
        p = _normalizar_pedido(order, client_data, shipping_data, items_customs)
        # Anota se a data foi sobrescrita
        if code in overrides and de_original != overrides[code]:
            p['data_entrega_original'] = de_original.isoformat() if de_original else None
            p['data_entrega_original_fmt'] = de_original.strftime('%d/%m/%Y') if de_original else ''
            p['data_entrega'] = de.isoformat()
            p['data_entrega_fmt'] = de.strftime('%d/%m/%Y')
            p['data_override'] = True
        else:
            p['data_override'] = False
        pedidos.append(p)

    pedidos.sort(key=lambda p: p.get('periodo') or '')
    resultado = {'pedidos': pedidos, 'total_janela': len(todos)}
    _pedidos_cache[cache_key] = (time.time(), resultado)
    return resultado


def contar_pedidos_por_dia(year, month, overrides=None):
    """overrides: dict {pedido_code: data_entrega} para sobrescrever a data extraida."""
    if not current_app.config.get('VNDA_API_TOKEN'):
        return {}

    overrides = overrides or {}

    first = date(year, month, 1)
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)

    start = first - timedelta(days=60)
    end = last + timedelta(days=5)
    todos = _buscar_pedidos_janela(start, end)

    contagem = {}
    for order in todos:
        if (order.get('status') or '').lower() in _STATUS_IGNORAR:
            continue
        code = order.get('code')
        de = overrides.get(code) or _extrair_data_entrega(order)
        if de and first <= de <= last:
            key = de.isoformat()
            contagem[key] = contagem.get(key, 0) + 1

    return contagem

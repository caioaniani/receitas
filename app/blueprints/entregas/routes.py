from datetime import date, datetime

from flask import render_template, request, jsonify, abort, current_app
from flask_login import login_required, current_user

import requests as http_requests

from app.blueprints.entregas import entregas_bp
from app.decorators import entrega_access_required
from app.extensions import db
from app.models import CartinhaEntrega
from app.services import vnda


@entregas_bp.route('/')
@login_required
@entrega_access_required
def index():
    return render_template('entregas/index.html', hoje=date.today().isoformat())


@entregas_bp.route('/api/pedidos')
@login_required
@entrega_access_required
def api_pedidos():
    data_str = request.args.get('data', date.today().isoformat())
    try:
        target = datetime.strptime(data_str, '%Y-%m-%d').date()
    except ValueError:
        target = date.today()

    resultado = vnda.buscar_pedidos_do_dia(target)

    if 'erro' in resultado:
        return jsonify(pedidos=[], data=data_str, erro=resultado['erro'])

    pedidos = resultado.get('pedidos', [])
    total_janela = resultado.get('total_janela', 0)

    codes = [p['code'] for p in pedidos if p['code']]
    cartinhas = {}
    if codes:
        for c in CartinhaEntrega.query.filter(CartinhaEntrega.pedido_code.in_(codes)).all():
            cartinhas[c.pedido_code] = c.texto or ''

    for p in pedidos:
        p['cartinha'] = cartinhas.get(p['code'], '')

    return jsonify(pedidos=pedidos, data=data_str, total_janela=total_janela)


@entregas_bp.route('/api/calendario')
@login_required
@entrega_access_required
def api_calendario():
    mes_str = request.args.get('mes', '')
    try:
        parts = mes_str.split('-')
        year, month = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        year, month = date.today().year, date.today().month

    dias = vnda.contar_pedidos_por_dia(year, month)
    return jsonify(dias=dias)


@entregas_bp.route('/cartinha/<code>', methods=['POST'])
@login_required
@entrega_access_required
def salvar_cartinha(code):
    data = request.get_json(silent=True) or {}
    texto = data.get('texto', '').strip()

    c = CartinhaEntrega.query.filter_by(pedido_code=code).first()
    if not c:
        c = CartinhaEntrega(pedido_code=code)
        db.session.add(c)

    c.texto = texto
    c.atualizado_em = datetime.utcnow()
    c.atualizado_por = current_user.id
    db.session.commit()

    return jsonify(ok=True)


@entregas_bp.route('/cartinha/<code>')
@login_required
@entrega_access_required
def get_cartinha(code):
    c = CartinhaEntrega.query.filter_by(pedido_code=code).first()
    if not c:
        return jsonify(texto='', atualizado_em=None, atualizado_por=None)
    return jsonify(
        texto=c.texto or '',
        atualizado_em=c.atualizado_em.isoformat() if c.atualizado_em else None,
        atualizado_por=c.autor.nome if c.autor else None,
    )


@entregas_bp.route('/api/debug')
@login_required
@entrega_access_required
def api_debug():
    """Diagnostico da conexao com a API Vnda."""
    token = current_app.config.get('VNDA_API_TOKEN', '')
    host = current_app.config.get('VNDA_SHOP_HOST', 'www.padariaartesanalonline.com.br')

    if token.lower().startswith('bearer '):
        token = token[7:]

    info = {
        'token_configurado': bool(token),
        'token_inicio': token[:8] + '...' if len(token) > 8 else '(vazio)',
        'host': host,
        'base_url': 'https://api.vnda.com.br/api/v2',
    }

    if not token:
        info['erro'] = 'VNDA_API_TOKEN nao configurado'
        return jsonify(info)

    headers = {
        'Authorization': f'Bearer {token}',
        'X-Shop-Host': host,
        'Accept': 'application/json',
        'User-Agent': 'OPaoPadaria/1.0',
    }

    try:
        resp = http_requests.get(
            'https://api.vnda.com.br/api/v2/orders',
            headers=headers,
            params={'per_page': 2},
            timeout=15,
        )
        info['status_code'] = resp.status_code
        info['response_headers'] = dict(resp.headers)

        try:
            body = resp.json()
            if isinstance(body, list):
                info['tipo_resposta'] = 'lista'
                info['quantidade'] = len(body)
                if body:
                    primeiro = body[0]
                    info['campos_pedido'] = list(primeiro.keys())
                    info['exemplo_code'] = primeiro.get('code', '')
                    info['exemplo_status'] = primeiro.get('status', '')
                    info['exemplo_expected_delivery'] = primeiro.get('expected_delivery_date', '')
                    info['exemplo_extra'] = primeiro.get('extra', {})
                    info['exemplo_client_id'] = primeiro.get('client_id', '')
                    info['exemplo_items_count'] = len(primeiro.get('items') or [])

                    detail_resp = http_requests.get(
                        'https://api.vnda.com.br/api/v2/orders/' + str(primeiro.get('code', '')),
                        headers=headers, timeout=15,
                    )
                    if detail_resp.status_code == 200:
                        try:
                            detail = detail_resp.json()
                            info['detalhe_campos'] = list(detail.keys())
                            info['detalhe_shipping_address'] = detail.get('shipping_address')
                            info['detalhe_client_name'] = detail.get('client_name', '')
                        except ValueError:
                            info['detalhe_erro'] = 'resposta nao-json'
            elif isinstance(body, dict):
                info['tipo_resposta'] = 'dict'
                info['chaves'] = list(body.keys())
                if 'results' in body:
                    info['quantidade'] = len(body['results'])
                    if body['results']:
                        primeiro = body['results'][0]
                        info['campos_pedido'] = list(primeiro.keys())
                        info['exemplo_code'] = primeiro.get('code', '')
                        info['exemplo_extra'] = primeiro.get('extra', {})
                elif 'error' in body or 'message' in body:
                    info['erro_api'] = body
            else:
                info['tipo_resposta'] = str(type(body))
                info['body_raw'] = str(body)[:500]
        except ValueError:
            info['resposta_texto'] = resp.text[:500]

    except http_requests.RequestException as e:
        info['erro_conexao'] = str(e)

    return jsonify(info)

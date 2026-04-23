from datetime import date, datetime

from flask import render_template, request, jsonify, abort
from flask_login import login_required, current_user

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

    pedidos = vnda.buscar_pedidos_do_dia(target)

    codes = [p['code'] for p in pedidos if p['code']]
    cartinhas = {}
    if codes:
        for c in CartinhaEntrega.query.filter(CartinhaEntrega.pedido_code.in_(codes)).all():
            cartinhas[c.pedido_code] = c.texto or ''

    for p in pedidos:
        p['cartinha'] = cartinhas.get(p['code'], '')

    return jsonify(pedidos=pedidos, data=data_str)


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

"""Vendas PDV via integracao Seru. Sob demanda — sem cache local."""
from datetime import date, datetime, timedelta

from flask import render_template, request, jsonify, current_app
from flask_login import login_required

from app.blueprints.pdv import pdv_bp
from app.decorators import admin_required
from app.services import seru


@pdv_bp.route('/')
@login_required
@admin_required
def index():
    return render_template('pdv/index.html', hoje=date.today().isoformat())


@pdv_bp.route('/api/vendas')
@login_required
@admin_required
def api_vendas():
    """Lista vendas Seru no intervalo. Default: hoje.

    ?inicio=YYYY-MM-DD&fim=YYYY-MM-DD
    """
    inicio_str = request.args.get('inicio') or date.today().isoformat()
    fim_str = request.args.get('fim') or inicio_str
    try:
        inicio = datetime.strptime(inicio_str, '%Y-%m-%d').date()
        fim = datetime.strptime(fim_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify(ok=False, erro='datas invalidas (use YYYY-MM-DD)'), 400

    if (fim - inicio).days > 92:
        return jsonify(ok=False, erro='intervalo maximo de 92 dias'), 400

    try:
        pedidos = seru.listar_pedidos_completo(inicio, fim)
    except Exception as e:
        current_app.logger.exception('Seru listar_pedidos falhou')
        return jsonify(ok=False, erro=f'{type(e).__name__}: {str(e)[:300]}'), 502

    def _f(v):
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    try:
        total = 0.0
        por_pagamento = {}
        por_canal = {}
        cancelados = 0
        for p in pedidos:
            if not isinstance(p, dict):
                continue
            if p.get('canceledAt'):
                cancelados += 1
                continue
            total += _f(p.get('total'))
            for pay in (p.get('payments') or []):
                if not isinstance(pay, dict):
                    continue
                metodo = pay.get('method') or pay.get('type') or '—'
                valor = _f(pay.get('value') or pay.get('total') or pay.get('amount'))
                por_pagamento[metodo] = por_pagamento.get(metodo, 0) + valor
            sc = p.get('salesChannel') or {}
            canal = (sc.get('name') if isinstance(sc, dict) else None) or '—'
            por_canal[canal] = por_canal.get(canal, 0) + _f(p.get('total'))
    except Exception as e:
        import traceback
        current_app.logger.exception('Erro agregando vendas Seru')
        return jsonify(
            ok=False,
            erro=f'{type(e).__name__} ao agregar: {str(e)[:300]}',
            traceback=traceback.format_exc().splitlines()[-5:],
            amostra_pedido=pedidos[0] if pedidos else None,
        ), 500

    try:
        return jsonify(
            ok=True,
            inicio=inicio.isoformat(),
            fim=fim.isoformat(),
            total_pedidos=len(pedidos),
            cancelados=cancelados,
            total_valor=total,
            por_pagamento=por_pagamento,
            por_canal=por_canal,
            pedidos=pedidos,
        )
    except Exception as e:
        current_app.logger.exception('Erro serializando resposta Seru')
        return jsonify(ok=False, erro=f'{type(e).__name__} no jsonify: {str(e)[:300]}'), 500


@pdv_bp.route('/api/vendas/<pedido_id>')
@login_required
@admin_required
def api_venda_detalhe(pedido_id):
    try:
        return jsonify(ok=True, pedido=seru.detalhes_pedido(pedido_id))
    except RuntimeError as e:
        return jsonify(ok=False, erro=str(e)[:300]), 502

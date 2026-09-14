"""Compra de kits com uma entrega por data escolhida pelo cliente."""
import json
import re
import secrets

from flask import abort, redirect, render_template, request, session, url_for

from app.blueprints.loja import loja_bp
from app.extensions import limiter
from app.models import CompraKit, KitCafe
from app.services import compra_kits, kits_cafe, loja_checkout
from app.utils import agora


@loja_bp.route('/kits-cafe')
def kits_catalogo():
    from app.blueprints.loja.routes import _em_teste
    cards = []
    for kit in kits_cafe.publicados():
        itens, erros = kits_cafe.montar(kit)
        if not erros:
            cards.append({'kit': kit, 'itens': itens, 'preco': kits_cafe.preco_kit(kit)})
    return render_template('loja/kits_catalogo.html', cards=cards, em_teste=_em_teste())


def _contexto(kit, form=None, agenda=None, erros=None):
    from app.blueprints.loja.routes import _ctx_checkout
    ctx = _ctx_checkout(erros=erros, form=form)
    itens, avisos = kits_cafe.montar(kit)
    raw = kits_cafe.itens_do_kit(kit) if not avisos else []
    base = agora()
    datas = loja_checkout.datas_disponiveis(
        'agendada', base=base, dias=compra_kits.DIAS_AGENDA_KITS,
        lead_dias=loja_checkout.lead_do_carrinho(raw))
    preco = sum((it['subtotal'] for it in itens), 0)
    tokens = dict(session.get('kits_checkout_tokens') or {})
    key = str(kit.id)
    if (key not in tokens or (request.method == 'GET'
            and CompraKit.query.filter_by(checkout_token=tokens[key]).first())):
        tokens[key] = secrets.token_hex(32)
        # Sessão continua pequena mesmo se o cliente visitar muitos kits.
        tokens = dict(list(tokens.items())[-10:])
        session['kits_checkout_tokens'] = tokens
    ctx.update(kit=kit, itens=itens, preco=preco, indisponivel=bool(avisos),
               agenda=agenda or [], checkout_token=tokens[key],
               data_min=datas[0].isoformat() if datas else '',
               data_max=datas[-1].isoformat() if datas else '',
               kits_config={
                   'precoCentavos': int(preco * 100),
                   'agenda': agenda or [],
                   'janelas': {d.isoformat(): loja_checkout.janelas_disponiveis(
                       'agendada', d, base=base) for d in datas},
                   # O servidor mantém as exceções das datas especiais.
                   # O navegador só escolhe o mapa conforme a distância.
                   'janelasLonge': {d.isoformat(): loja_checkout.janelas_disponiveis(
                       'agendada', d, base=base,
                       distancia_km=loja_checkout.DISTANCIA_CORTE_PRIMEIRA_JANELA_KM)
                       for d in datas},
                   'cepUrl': url_for('loja.api_cep', cep='00000000'),
                   'freteUrl': url_for('loja.api_frete'),
                   'corteKm': loja_checkout.DISTANCIA_CORTE_PRIMEIRA_JANELA_KM,
               })
    return ctx


@loja_bp.route('/kits-cafe/<int:kit_id>', methods=['GET', 'POST'])
@limiter.limit('30 per minute')
def kit_comprar(kit_id):
    kit = KitCafe.query.filter_by(id=kit_id, ativo=True).first_or_404()
    if request.method == 'GET':
        return render_template('loja/kit_comprar.html', **_contexto(kit))
    token = request.form.get('checkout_token', '')
    if not re.fullmatch(r'[0-9a-f]{64}', token) or not secrets.compare_digest(
            token, (session.get('kits_checkout_tokens') or {}).get(str(kit.id), '')):
        abort(400, description='Reabra o kit para iniciar a compra.')
    try:
        agenda = json.loads(request.form.get('agenda_json') or '[]')
    except (ValueError, TypeError):
        agenda = []
    compra, erros = compra_kits.criar_compra(
        kit, request.form, agenda, checkout_token=token)
    if erros:
        validos = agenda if isinstance(agenda, list) and len(agenda) <= 31 else []
        return render_template('loja/kit_comprar.html',
                               **_contexto(kit, request.form, validos, erros)), 400
    return redirect(url_for('loja.pedido_pagamento', codigo=compra.pedido_principal.codigo))

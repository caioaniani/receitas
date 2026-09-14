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
            sucos = kits_cafe.opcoes_suco(kit)
            cards.append({'kit': kit, 'itens': _itens_fixos(itens, sucos),
                          'sucos': sucos, 'preco': sum(it['subtotal'] for it in itens),
                          'preco_variavel': len({s['preco'] for s in sucos}) > 1})
    return render_template('loja/kits_catalogo.html', cards=cards, em_teste=_em_teste())


def _itens_fixos(itens, sucos):
    ids = {suco['id'] for suco in sucos}
    return [item for item in itens if item.get('produto_id') not in ids]


def _calendario(raw, base, cache):
    lead_dias = loja_checkout.lead_do_carrinho(raw)
    if lead_dias in cache:
        return cache[lead_dias]
    datas = loja_checkout.datas_disponiveis(
        'agendada', base=base, dias=compra_kits.DIAS_AGENDA_KITS,
        lead_dias=lead_dias)
    cache[lead_dias] = {
        'dataMin': datas[0].isoformat() if datas else '',
        'dataMax': datas[-1].isoformat() if datas else '',
        'janelas': {d.isoformat(): loja_checkout.janelas_disponiveis(
            'agendada', d, base=base) for d in datas},
        # O servidor mantém as exceções das datas especiais, inclusive longe.
        'janelasLonge': {d.isoformat(): loja_checkout.janelas_disponiveis(
            'agendada', d, base=base,
            distancia_km=loja_checkout.DISTANCIA_CORTE_PRIMEIRA_JANELA_KM)
            for d in datas},
    }
    return cache[lead_dias]


def _contexto(kit, form=None, agenda=None, erros=None):
    from app.blueprints.loja.routes import _ctx_checkout
    ctx = _ctx_checkout(erros=erros, form=form)
    try:
        sucos = kits_cafe.opcoes_suco(kit)
    except ValueError:
        sucos = []
    escolha = (form or {}).get('suco_id', '')
    suco_id = next((s['id'] for s in sucos if str(s['id']) == escolha), None)
    itens, avisos = kits_cafe.montar(kit, suco_id=suco_id)
    fixos = _itens_fixos(itens, sucos)
    # A agenda só consulta kind/id para calcular a antecedência. Reutilizar
    # os itens já validados evita validar o grupo inteiro para cada opção.
    raw_fixos = [{'kind': it['kind'], 'id': it['id']} for it in fixos]
    base = agora()
    calendarios = {}
    calendario = _calendario(itens, base, calendarios)
    preco = sum((it['subtotal'] for it in itens), 0)
    preco_fixo = sum((it['subtotal'] for it in fixos), 0)
    escolhas = {}
    for suco in sucos:
        escolhas[str(suco['id'])] = {
            **_calendario([*raw_fixos, {'kind': 'produto', 'id': suco['id']}], base, calendarios),
            'precoCentavos': int((preco_fixo + suco['preco']) * 100),
        }
    tokens = dict(session.get('kits_checkout_tokens') or {})
    key = str(kit.id)
    if (key not in tokens or (request.method == 'GET'
            and CompraKit.query.filter_by(checkout_token=tokens[key]).first())):
        tokens[key] = secrets.token_hex(32)
        # Sessão continua pequena mesmo se o cliente visitar muitos kits.
        tokens = dict(list(tokens.items())[-10:])
        session['kits_checkout_tokens'] = tokens
    ctx.update(kit=kit, itens=fixos, preco=preco,
               sucos=sucos, suco_id=suco_id,
               preco_variavel=len({s['preco'] for s in sucos}) > 1,
               indisponivel=bool(avisos),
               agenda=agenda or [], checkout_token=tokens[key],
               data_min=calendario['dataMin'], data_max=calendario['dataMax'],
               kits_config={
                   **calendario,
                   'precoCentavos': int(preco * 100),
                   'sucos': escolhas,
                   'agenda': agenda or [],
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

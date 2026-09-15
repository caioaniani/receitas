"""Compra de kits com uma entrega por data escolhida pelo cliente."""
import json
import re
import secrets
from itertools import product

from flask import abort, redirect, render_template, request, session, url_for
from sqlalchemy.orm import selectinload

from app.blueprints.loja import loja_bp
from app.extensions import limiter
from app.models import CompraKit, KitCafe
from app.services import compra_kits, kits_adicionais, kits_cafe, kits_fotos, loja_checkout, loja_leitura
from app.utils import agora


@loja_bp.app_template_filter('texto_opcao_cafe')
def texto_opcao_cafe(texto):
    """Vocabulário público, inclusive nomes antigos cadastrados pelo owner."""
    def trocar(match):
        palavra = 'opções' if match[0].lower() == 'planos' else 'opção'
        return palavra.upper() if match[0].isupper() else (
            palavra.capitalize() if match[0][0].isupper() else palavra)
    return re.sub(r'\bplanos?\b', trocar, str(texto or ''), flags=re.I)


@loja_bp.route('/kits-cafe')
def kits_catalogo():
    return redirect(url_for('loja.home', _anchor='cat-kits-cafe'), code=301)


def cards_catalogo():
    """Cards da home, com a validação canônica e o catálogo do lote ativo."""
    cards = []
    kits = (KitCafe.query.options(
        selectinload(KitCafe.itens), selectinload(KitCafe.sucos),
        selectinload(KitCafe.opcoes), selectinload(KitCafe.fotos))
        .filter_by(ativo=True).order_by(KitCafe.id).all())
    for kit in kits:
        itens, erros = kits_cafe.montar(kit)
        if not erros:
            sucos = kits_cafe.opcoes_suco(kit)
            grupos = kits_cafe.opcoes_grupos(kit)
            cards.append({'kit': kit, 'itens': _itens_fixos(itens, sucos, grupos),
                          'grupos': grupos,
                          'sucos': sucos, 'preco': sum(it['subtotal'] for it in itens),
                          'preco_variavel': _preco_variavel(sucos, grupos)})
    for card in cards:
        card['itens_fotos'] = kits_fotos.candidatos(card['itens'], card['sucos'], card['grupos'])
    leitura = loja_leitura.atual()
    capas = ({chave: item.get('imagem') or ''
              for chave, item in leitura['catalogo'].items()} if leitura is not None
             else kits_fotos.capas_dos_itens(
                 [item for card in cards for item in card['itens_fotos']]))
    for card in cards:
        card['imagens'] = kits_fotos.imagens_do_kit(
            card['kit'], card['itens'], card['itens_fotos'], capas)
    return cards


def _preco_variavel(sucos, grupos):
    return (len({s['preco'] for s in sucos}) > 1
            or any(len({o['preco'] for o in g['opcoes']}) > 1 for g in grupos))


def _itens_fixos(itens, sucos, grupos=()):
    ids = {suco['id'] for suco in sucos}
    opcionais = {(o['kind'], o['id']) for g in grupos for o in g['opcoes']}
    return [item for item in itens if item.get('produto_id') not in ids
            and (item['kind'], item['id']) not in opcionais]


def _calendario(raw, base, cache):
    lead_dias = loja_checkout.lead_do_carrinho(raw)
    if lead_dias in cache:
        return cache[lead_dias]
    datas = loja_checkout.datas_disponiveis(
        'agendada', base=base, dias=compra_kits.DIAS_AGENDA_KITS,
        lead_dias=lead_dias)
    cache[lead_dias] = {
        'leadDias': lead_dias,
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
        grupos = kits_cafe.opcoes_grupos(kit)
    except ValueError:
        sucos = []
        grupos = []
    escolha = (form or {}).get('suco_id', '')
    suco_id = next((s['id'] for s in sucos if str(s['id']) == escolha), None)
    escolhas_selecionadas = {
        g['chave']: (form or {}).get(f'escolha_{g["chave"]}', '') for g in grupos}
    escolhas_completas = all(escolhas_selecionadas[g['chave']] in {
        o['chave'] for o in g['opcoes']} for g in grupos)
    itens, avisos = kits_cafe.montar(
        kit, suco_id=suco_id, escolhas=escolhas_selecionadas if escolhas_completas else None)
    fixos = _itens_fixos(itens, sucos, grupos)
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
            'leadDias': _calendario(
                [*raw_fixos, {'kind': 'produto', 'id': suco['id']}], base, calendarios)['leadDias'],
            'precoCentavos': int((preco_fixo + suco['preco']) * 100),
        }
    combinacoes = {}
    if grupos and not avisos:
        # Preparar as escolhas já limita o produto cartesiano a 64 combinações.
        # Cada calendário é reutilizado pela antecedência, sem consultar frete.
        for combinacao in product(sucos or [{'id': '', 'preco': 0}],
                                   *(g['opcoes'] for g in grupos)):
            suco, *opcoes = combinacao
            raw = [*raw_fixos]
            if suco['id']:
                raw.append({'kind': 'produto', 'id': suco['id']})
            raw.extend({'kind': o['kind'], 'id': o['id']} for o in opcoes)
            chave = '|'.join([str(suco['id']), *(o['chave'] for o in opcoes)])
            combinacoes[chave] = {
                'leadDias': _calendario(raw, base, calendarios)['leadDias'],
                'precoCentavos': int((preco_fixo + suco['preco']
                                      + sum(o['preco'] for o in opcoes)) * 100),
            }
    selecionados, _ = kits_adicionais.ler(form or {}, conferir_catalogo=False)
    adicionais = kits_adicionais.catalogo(base=base, selecionados=selecionados) if not avisos else []
    for adicional in adicionais:
        adicional['leadDias'] = _calendario([adicional], base, calendarios)['leadDias']
    tokens = dict(session.get('kits_checkout_tokens') or {})
    key = str(kit.id)
    if (key not in tokens or (request.method == 'GET'
            and CompraKit.query.filter_by(checkout_token=tokens[key]).first())):
        tokens[key] = secrets.token_hex(32)
        # Sessão continua pequena mesmo se o cliente visitar muitos kits.
        tokens = dict(list(tokens.items())[-10:])
        session['kits_checkout_tokens'] = tokens
    itens_fotos = kits_fotos.candidatos(fixos, sucos, grupos)
    ctx.update(kit=kit, itens=fixos, preco=preco, adicionais=adicionais,
               tem_adicionais=bool(adicionais or selecionados),
               kit_imagens=kits_fotos.imagens_do_kit(
                   kit, fixos, itens_fotos, kits_fotos.capas_dos_itens(itens_fotos)),
               sucos=sucos, suco_id=suco_id,
               grupos=grupos, escolhas_selecionadas=escolhas_selecionadas,
               escolhas_completas=escolhas_completas,
               preco_variavel=_preco_variavel(sucos, grupos),
               indisponivel=bool(avisos),
               agenda=agenda or [], checkout_token=tokens[key],
               data_min=calendario['dataMin'], data_max=calendario['dataMax'],
               kits_config={
                   **calendario,
                   'precoCentavos': int(preco * 100),
                   'sucos': escolhas,
                   'grupos': [g['chave'] for g in grupos],
                   'combinacoes': combinacoes,
                   'adicionais': adicionais,
                   'adicionaisSelecionados': selecionados,
                   'calendarios': calendarios,
                   'maxAdicionais': kits_adicionais.MAX_ITENS,
                   'agenda': agenda or [],
                   'cepUrl': url_for('loja.api_cep', cep='00000000'),
                   'freteUrl': url_for('loja.api_frete'),
                   'corteKm': loja_checkout.DISTANCIA_CORTE_PRIMEIRA_JANELA_KM,
               })
    return ctx


def _render_compra(kit, form=None, agenda=None, erros=None):
    # O escopo termina antes de criar/reservar/cobrar. POST inválido só o usa
    # depois da validação e do rollback, para reapresentar dados atuais.
    with loja_leitura.catalogo_em_lote(dias=compra_kits.DIAS_AGENDA_KITS):
        return render_template('loja/kit_comprar.html', **_contexto(kit, form, agenda, erros))


@loja_bp.route('/kits-cafe/<int:kit_id>', methods=['GET', 'POST'])
@limiter.limit('30 per minute')
def kit_comprar(kit_id):
    kit = KitCafe.query.filter_by(id=kit_id, ativo=True).first_or_404()
    if request.method == 'GET':
        return _render_compra(kit)
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
        return _render_compra(kit, request.form, validos, erros), 400
    return redirect(url_for('loja.pedido_pagamento', codigo=compra.pedido_principal.codigo))

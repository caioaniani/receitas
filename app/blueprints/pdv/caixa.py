"""Frente de caixa do PDV próprio, com captura de cartão na Clover Mini.

Fluxo: o operador monta o carrinho → POST cria a Venda → cada forma de
pagamento vira um VendaPagamento. Cartão (débito/crédito) com a Clover
configurada dispara a cobrança na maquininha em uma thread de background
(a chamada bloqueia até o cliente concluir) e o frontend faz polling do
status da venda. Quando a soma dos pagamentos aprovados cobre o total,
a venda vira 'paga'.
"""
import threading
import uuid
from datetime import datetime, date, time as dtime, timezone, timedelta

from flask import render_template, request, jsonify, current_app
from flask_login import login_required, current_user
from sqlalchemy.exc import IntegrityError

from app.blueprints.pdv import pdv_bp
from app.decorators import loja_access_required
from app.extensions import db
from app.models import (Venda, VendaItem, VendaPagamento, Receita, Produto,
                        Loja, PrecoLojaReceita)
from app.services import clover

# Fuso de Sao Paulo (Brasil nao tem horario de verao desde 2019).
BRT = timezone(timedelta(hours=-3))

METODOS = ('dinheiro', 'pix', 'debito', 'credito')
METODOS_CARTAO = ('debito', 'credito')


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _agora_brt():
    return datetime.now(BRT)


# ── Telas ──

@pdv_bp.route('/caixa')
@login_required
@loja_access_required
def caixa():
    lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    return render_template(
        'pdv/caixa.html',
        lojas=lojas,
        loja_usuario_id=current_user.loja_id,
        clover_ativo=clover.ativo(),
        clover_modo=clover.modo(),
    )


# ── Catálogo ──

def _precos_loja(loja_id):
    if not loja_id:
        return {}
    return {p.receita_id: p.preco
            for p in PrecoLojaReceita.query.filter_by(loja_id=loja_id).all()}


def _preco_receita(r, overrides):
    return overrides.get(r.id) or r.preco_loja or r.preco_venda


def _preco_produto(p):
    return p.preco_loja or p.preco_site or p.preco_atacado


@pdv_bp.route('/caixa/api/catalogo')
@login_required
@loja_access_required
def caixa_catalogo():
    """Itens vendáveis (receitas + produtos com preço) na loja informada."""
    loja_id = request.args.get('loja_id', type=int) or current_user.loja_id
    overrides = _precos_loja(loja_id)
    itens = []
    for r in Receita.query.order_by(Receita.categoria, Receita.nome).all():
        preco = _preco_receita(r, overrides)
        if preco and preco > 0:
            itens.append({'tipo': 'receita', 'id': r.id, 'nome': r.nome,
                          'categoria': r.categoria or 'Outros', 'preco': round(preco, 2)})
    for p in Produto.query.filter(Produto.ativo.isnot(False)) \
                          .order_by(Produto.categoria, Produto.nome).all():
        preco = _preco_produto(p)
        if preco and preco > 0:
            itens.append({'tipo': 'produto', 'id': p.id, 'nome': p.nome,
                          'categoria': p.categoria or 'Cestas', 'preco': round(preco, 2)})
    return jsonify(ok=True, itens=itens, loja_id=loja_id)


# ── Venda ──

def _venda_dict(v):
    return {
        'id': v.id,
        'code': v.code,
        'status': v.status,
        'loja_id': v.loja_id,
        'subtotal': v.subtotal,
        'desconto': v.desconto,
        'total': v.total,
        'total_pago': v.total_pago,
        'restante': v.restante,
        'criado_em': v.criado_em.isoformat() if v.criado_em else None,
        'itens': [{
            'descricao': i.descricao,
            'quantidade': i.quantidade,
            'preco_unitario': i.preco_unitario,
            'subtotal': i.subtotal,
        } for i in v.itens],
        'pagamentos': [{
            'id': p.id,
            'metodo': p.metodo,
            'valor': p.valor,
            'valor_recebido': p.valor_recebido,
            'troco': p.troco,
            'status': p.status,
            'capturado_via': p.capturado_via,
            'erro': p.erro,
        } for p in v.pagamentos],
    }


def _gerar_code():
    prefixo = 'V' + _agora_brt().strftime('%Y%m%d')
    ultimo = Venda.query.filter(Venda.code.like(f'{prefixo}-%')) \
                        .order_by(Venda.id.desc()).first()
    seq = 1
    if ultimo:
        try:
            seq = int(ultimo.code.rsplit('-', 1)[1]) + 1
        except (ValueError, IndexError):
            seq = 1
    return f'{prefixo}-{seq:03d}'


@pdv_bp.route('/caixa/api/vendas', methods=['POST'])
@login_required
@loja_access_required
def caixa_criar_venda():
    data = request.get_json(silent=True) or {}
    loja_id = data.get('loja_id') or current_user.loja_id
    if loja_id and not db.session.get(Loja, loja_id):
        return jsonify(ok=False, erro='loja inválida'), 400

    itens_req = data.get('itens') or []
    if not itens_req:
        return jsonify(ok=False, erro='venda sem itens'), 400
    if len(itens_req) > 100:
        return jsonify(ok=False, erro='máximo de 100 itens por venda'), 400

    overrides = _precos_loja(loja_id)
    itens = []
    subtotal = 0.0
    for it in itens_req:
        if not isinstance(it, dict):
            return jsonify(ok=False, erro='item inválido'), 400
        tipo = it.get('tipo')
        qtd = _num(it.get('quantidade')) or 1
        if qtd <= 0 or qtd > 9999:
            return jsonify(ok=False, erro='quantidade inválida'), 400
        # Preço de catálogo sempre resolvido no servidor; só item avulso
        # aceita preço digitado pelo operador.
        if tipo == 'receita':
            r = db.session.get(Receita, int(it.get('id') or 0))
            preco = _preco_receita(r, overrides) if r else None
            if not r or not preco:
                return jsonify(ok=False, erro=f'receita {it.get("id")} sem preço de venda'), 400
            item = VendaItem(receita_id=r.id, descricao=r.nome,
                             quantidade=qtd, preco_unitario=round(preco, 2))
        elif tipo == 'produto':
            p = db.session.get(Produto, int(it.get('id') or 0))
            preco = _preco_produto(p) if p else None
            if not p or not preco:
                return jsonify(ok=False, erro=f'produto {it.get("id")} sem preço de venda'), 400
            item = VendaItem(produto_id=p.id, descricao=p.nome,
                             quantidade=qtd, preco_unitario=round(preco, 2))
        elif tipo == 'avulso':
            descricao = (it.get('descricao') or '').strip()[:200]
            preco = _num(it.get('preco_unitario'))
            if not descricao or not preco or preco <= 0:
                return jsonify(ok=False, erro='item avulso precisa de descrição e preço'), 400
            item = VendaItem(descricao=descricao, quantidade=qtd,
                             preco_unitario=round(preco, 2))
        else:
            return jsonify(ok=False, erro=f'tipo de item inválido: {tipo}'), 400
        item.subtotal = round(item.quantidade * item.preco_unitario, 2)
        subtotal += item.subtotal
        itens.append(item)

    subtotal = round(subtotal, 2)
    desconto = round(_num(data.get('desconto')) or 0, 2)
    if desconto < 0 or desconto >= subtotal + 0.005:
        return jsonify(ok=False, erro='desconto inválido'), 400

    venda = Venda(
        loja_id=loja_id,
        usuario_id=current_user.id,
        subtotal=subtotal,
        desconto=desconto,
        total=round(subtotal - desconto, 2),
        observacao=(data.get('observacao') or '').strip()[:300] or None,
        itens=itens,
    )
    # code é unique — em colisão (dois caixas criando junto) tenta de novo
    for _ in range(3):
        venda.code = _gerar_code()
        db.session.add(venda)
        try:
            db.session.commit()
            break
        except IntegrityError:
            db.session.rollback()
    else:
        return jsonify(ok=False, erro='não consegui gerar o código da venda, tente de novo'), 500

    return jsonify(ok=True, venda=_venda_dict(venda))


@pdv_bp.route('/caixa/api/vendas/<int:venda_id>')
@login_required
@loja_access_required
def caixa_venda(venda_id):
    venda = db.session.get(Venda, venda_id)
    if not venda:
        return jsonify(ok=False, erro='venda não encontrada'), 404
    return jsonify(ok=True, venda=_venda_dict(venda))


@pdv_bp.route('/caixa/api/vendas/<int:venda_id>/cancelar', methods=['POST'])
@login_required
@loja_access_required
def caixa_cancelar_venda(venda_id):
    venda = db.session.get(Venda, venda_id)
    if not venda:
        return jsonify(ok=False, erro='venda não encontrada'), 404
    if venda.status != 'aberta':
        return jsonify(ok=False, erro=f'venda já está {venda.status}'), 400
    if any(p.status == 'aguardando_clover' for p in venda.pagamentos):
        return jsonify(ok=False, erro='cancele primeiro o pagamento na maquininha'), 409
    if venda.total_pago > 0:
        return jsonify(ok=False, erro='venda tem pagamento aprovado — estorne na '
                                      'maquininha e cancele com um admin'), 409
    venda.status = 'cancelada'
    venda.finalizado_em = datetime.utcnow()
    db.session.commit()
    return jsonify(ok=True, venda=_venda_dict(venda))


# ── Pagamentos ──

def _finalizar_se_paga(venda):
    if venda.status == 'aberta' and venda.restante <= 0.009:
        venda.status = 'paga'
        venda.finalizado_em = datetime.utcnow()


def _processar_clover(app, pagamento_id):
    """Roda em thread de background: chama a Clover (bloqueante) e grava o
    resultado. Só atualiza se o pagamento ainda estiver aguardando — o
    operador pode ter cancelado enquanto a maquininha processava."""
    with app.app_context():
        pag = db.session.get(VendaPagamento, pagamento_id)
        if not pag or pag.status != 'aguardando_clover':
            return
        valor_centavos = int(round((pag.valor or 0) * 100))
        external_id = pag.clover_external_id
        try:
            res = clover.criar_pagamento(valor_centavos, external_id)
        except Exception as e:
            current_app.logger.exception('Clover: pagamento %s falhou', pagamento_id)
            db.session.rollback()
            pag = db.session.get(VendaPagamento, pagamento_id)
            if pag and pag.status == 'aguardando_clover':
                pag.status = 'erro'
                pag.erro = str(e)[:300]
                db.session.commit()
            return

        db.session.refresh(pag)
        pag.clover_payment_id = res.get('payment_id')
        pag.clover_resposta = clover.resposta_json(res.get('raw'))
        if pag.status != 'aguardando_clover':
            # Operador cancelou no caixa durante o processamento.
            if res.get('aprovado'):
                # A maquininha capturou mesmo assim — registra pra não sumir dinheiro.
                pag.status = 'aprovado'
                pag.erro = 'aprovado na maquininha após cancelamento no caixa — confira'
                _finalizar_se_paga(pag.venda)
                current_app.logger.warning(
                    'Clover: pagamento %s aprovado após cancelamento no caixa', pagamento_id)
            db.session.commit()
            return
        if res.get('aprovado'):
            pag.status = 'aprovado'
            pag.erro = None
            _finalizar_se_paga(pag.venda)
        else:
            pag.status = 'negado'
            pag.erro = (res.get('mensagem') or 'pagamento não aprovado')[:300]
        db.session.commit()


@pdv_bp.route('/caixa/api/vendas/<int:venda_id>/pagamentos', methods=['POST'])
@login_required
@loja_access_required
def caixa_add_pagamento(venda_id):
    venda = db.session.get(Venda, venda_id)
    if not venda:
        return jsonify(ok=False, erro='venda não encontrada'), 404
    if venda.status != 'aberta':
        return jsonify(ok=False, erro=f'venda já está {venda.status}'), 400
    if any(p.status == 'aguardando_clover' for p in venda.pagamentos):
        return jsonify(ok=False, erro='já existe um pagamento em andamento na maquininha'), 409

    data = request.get_json(silent=True) or {}
    metodo = (data.get('metodo') or '').strip().lower()
    if metodo not in METODOS:
        return jsonify(ok=False, erro=f'método inválido (use {", ".join(METODOS)})'), 400

    restante = venda.restante
    valor = _num(data.get('valor'))
    valor = round(valor if valor is not None else restante, 2)
    if valor <= 0:
        return jsonify(ok=False, erro='valor inválido'), 400
    if valor > restante + 0.005:
        return jsonify(ok=False, erro=f'valor maior que o restante (R$ {restante:.2f})'), 400

    pag = VendaPagamento(metodo=metodo, valor=valor)
    # Anexa na relação (não só via venda_id) pra coleção venda.pagamentos
    # já carregada enxergar o registro novo em total_pago/restante.
    venda.pagamentos.append(pag)

    if metodo == 'dinheiro':
        recebido = _num(data.get('valor_recebido'))
        recebido = round(recebido if recebido is not None else valor, 2)
        if recebido < valor - 0.005:
            return jsonify(ok=False, erro='valor recebido menor que o valor a pagar'), 400
        pag.valor_recebido = recebido
        pag.troco = round(recebido - valor, 2)

    if metodo in METODOS_CARTAO and clover.ativo():
        pag.status = 'aguardando_clover'
        pag.capturado_via = clover.modo()
        # externalPaymentId: nosso identificador na Clover (conciliação)
        pag.clover_external_id = uuid.uuid4().hex
        db.session.commit()
        app = current_app._get_current_object()
        threading.Thread(target=_processar_clover, args=(app, pag.id),
                         daemon=True).start()
        return jsonify(ok=True, aguardando=True, pagamento_id=pag.id,
                       venda=_venda_dict(venda))

    # Dinheiro, PIX (QR da loja) ou cartão sem integração: registro manual
    pag.status = 'aprovado'
    pag.capturado_via = 'manual'
    _finalizar_se_paga(venda)
    db.session.commit()
    return jsonify(ok=True, aguardando=False, pagamento_id=pag.id,
                   venda=_venda_dict(venda))


@pdv_bp.route('/caixa/api/vendas/<int:venda_id>/pagamentos/<int:pag_id>/cancelar',
              methods=['POST'])
@login_required
@loja_access_required
def caixa_cancelar_pagamento(venda_id, pag_id):
    pag = db.session.get(VendaPagamento, pag_id)
    if not pag or pag.venda_id != venda_id:
        return jsonify(ok=False, erro='pagamento não encontrado'), 404
    if pag.status != 'aguardando_clover':
        return jsonify(ok=False, erro=f'pagamento está {pag.status}, não dá pra cancelar'), 400
    res = clover.cancelar_operacao()
    pag.status = 'cancelado'
    pag.erro = None if res.get('ok') else f"cancelado no caixa; maquininha: {res.get('detalhe')}"[:300]
    db.session.commit()
    return jsonify(ok=True, venda=_venda_dict(pag.venda))


# ── Vendas do dia ──

@pdv_bp.route('/caixa/api/vendas-dia')
@login_required
@loja_access_required
def caixa_vendas_dia():
    """Vendas de hoje (BRT) — painel lateral do caixa."""
    dia_str = request.args.get('dia')
    try:
        dia = date.fromisoformat(dia_str) if dia_str else _agora_brt().date()
    except ValueError:
        return jsonify(ok=False, erro='dia inválido (use YYYY-MM-DD)'), 400
    # criado_em é UTC naive; converte o intervalo do dia BRT pra UTC
    ini = datetime.combine(dia, dtime.min, tzinfo=BRT).astimezone(timezone.utc).replace(tzinfo=None)
    fim = datetime.combine(dia, dtime.max, tzinfo=BRT).astimezone(timezone.utc).replace(tzinfo=None)
    vendas = Venda.query.filter(Venda.criado_em >= ini, Venda.criado_em <= fim) \
                        .order_by(Venda.id.desc()).limit(200).all()
    total = 0.0
    por_metodo = {}
    for v in vendas:
        if v.status != 'paga':
            continue
        total += v.total or 0
        for p in v.pagamentos:
            if p.status == 'aprovado':
                por_metodo[p.metodo] = round(por_metodo.get(p.metodo, 0) + (p.valor or 0), 2)
    return jsonify(ok=True, dia=dia.isoformat(),
                   total_pagas=round(total, 2), por_metodo=por_metodo,
                   vendas=[_venda_dict(v) for v in vendas])


# ── Status da maquininha ──

@pdv_bp.route('/caixa/api/clover/status')
@login_required
@loja_access_required
def caixa_clover_status():
    return jsonify(ok=True, ativo=clover.ativo(), modo=clover.modo(),
                   ping=clover.ping())

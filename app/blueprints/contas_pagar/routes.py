"""Contas a pagar — geradas a partir de fotos de NF/boleto no Slack.

A IA faz a primeira extracao; aqui o usuario confere, edita e marca pago.
Documento original sempre no Dropbox (imagem_url).
"""
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.contas_pagar import contas_pagar_bp
from app.decorators import admin_required, owner_required
from app.extensions import db
from app.models import ContaPagar, Fornecedor
from app.utils import agora

# Abas por status (slug, label, status no banco).
ABAS = (
    ('aberto', 'Em aberto', 'aberto'),
    ('pago', 'Pagos', 'pago'),
    ('ignorado', 'Ignorados', 'ignorado'),
)


def _parse_valor(raw):
    """Parseia valor do form. Aceita ponto (input number) ou virgula (BR)."""
    if raw is None or str(raw).strip() == '':
        return None
    s = str(raw).strip()
    if ',' in s:  # formato BR: tira milhar, troca decimal
        s = s.replace('.', '').replace(',', '.')
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _parse_data(raw):
    if not raw:
        return None
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        return None


def _mapa_canais(contas):
    """{id_canal: '#nome'} pros canais distintos das contas. Cacheado no
    cliente Slack; fallback pro ID se nao resolver."""
    from app.services import slack as slack_api
    mapa = {}
    for c in contas:
        cid = c.origem_canal
        if cid and cid not in mapa:
            mapa[cid] = slack_api.nome_canal(cid)
    return mapa


@contas_pagar_bp.route('/')
@login_required
@admin_required
def lista():
    from sqlalchemy import func

    aba = request.args.get('aba', 'aberto')
    if aba not in {s for s, _, _ in ABAS}:
        aba = 'aberto'
    status_filtro = {s: st for s, _, st in ABAS}[aba]

    cont = dict(db.session.query(ContaPagar.status, func.count())
                .group_by(ContaPagar.status).all())
    contagens = {slug: cont.get(st, 0) for slug, _, st in ABAS}

    contas = (ContaPagar.query
              .filter(ContaPagar.status == status_filtro)
              .order_by(ContaPagar.vencimento.is_(None),
                        ContaPagar.vencimento.asc(),
                        ContaPagar.criado_em.desc())
              .limit(200).all())
    return render_template('contas_pagar/lista.html', contas=contas,
                           abas=ABAS, aba_atual=aba, contagens=contagens,
                           canais_nome=_mapa_canais(contas))


@contas_pagar_bp.route('/<int:id>')
@login_required
@admin_required
def detalhe(id):
    conta = ContaPagar.query.get_or_404(id)
    itens = []
    if conta.itens_json:
        try:
            itens = json.loads(conta.itens_json)
        except (json.JSONDecodeError, TypeError):
            itens = []
    fornecedores = Fornecedor.query.filter_by(ativo=True).order_by(Fornecedor.nome).all()
    # Candidatos pra vincular (mesmo... outro documento ainda nao relacionado)
    relacionaveis = (ContaPagar.query
                     .filter(ContaPagar.id != conta.id)
                     .filter(ContaPagar.status != 'ignorado')
                     .order_by(ContaPagar.criado_em.desc())
                     .limit(50).all())
    return render_template('contas_pagar/detalhe.html', conta=conta, itens=itens,
                           fornecedores=fornecedores, relacionaveis=relacionaveis)


@contas_pagar_bp.route('/<int:id>/editar', methods=['POST'])
@login_required
@admin_required
def editar(id):
    conta = ContaPagar.query.get_or_404(id)
    conta.fornecedor_nome = (request.form.get('fornecedor_nome') or '').strip() or None
    fid = request.form.get('fornecedor_id')
    conta.fornecedor_id = int(fid) if fid and fid.isdigit() else None
    conta.tipo_documento = request.form.get('tipo_documento') or conta.tipo_documento
    conta.valor_total = _parse_valor(request.form.get('valor_total'))
    conta.vencimento = _parse_data(request.form.get('vencimento'))
    conta.nf_numero = (request.form.get('nf_numero') or '').strip() or None
    conta.codigo_barras = (request.form.get('codigo_barras') or '').strip() or None
    conta.linha_digitavel = (request.form.get('linha_digitavel') or '').strip() or None
    conta.info_pagamento = (request.form.get('info_pagamento') or '').strip() or None
    rel = request.form.get('relacionado_id')
    conta.relacionado_id = int(rel) if rel and rel.isdigit() else None
    conta.editado_em = agora()
    conta.editado_por_id = current_user.id
    db.session.commit()
    flash('Conta atualizada.', 'success')
    return redirect(url_for('contas_pagar.detalhe', id=id))


@contas_pagar_bp.route('/<int:id>/pagar', methods=['POST'])
@login_required
@admin_required
def pagar(id):
    conta = ContaPagar.query.get_or_404(id)
    conta.status = 'pago'
    conta.valor_pago = conta.valor_total
    conta.pago_em = agora()
    conta.forma_pagamento = (request.form.get('forma_pagamento') or '').strip() or None
    conta.editado_em = agora()
    conta.editado_por_id = current_user.id
    db.session.commit()
    flash('Conta marcada como paga.', 'success')
    return redirect(url_for('contas_pagar.detalhe', id=id))


@contas_pagar_bp.route('/importar-historico', methods=['POST'])
@login_required
@owner_required
def importar_historico():
    """Varre os ultimos 30 dias dos canais de NF e cria as contas. Roda em
    background (a IA por imagem demora). As contas aparecem aos poucos."""
    import threading

    from app.services import conta_pagar_slack

    app_obj = current_app._get_current_object()

    def _runner():
        try:
            conta_pagar_slack.importar_historico(app_obj, dias=30)
        except Exception:
            app_obj.logger.exception('importar_historico falhou')

    threading.Thread(target=_runner, daemon=True).start()
    flash('Importacao do historico (30 dias) iniciada. As contas vao aparecer '
          'aqui conforme forem processadas.', 'info')
    return redirect(url_for('contas_pagar.lista'))


@contas_pagar_bp.route('/<int:id>/status', methods=['POST'])
@login_required
@admin_required
def mudar_status(id):
    conta = ContaPagar.query.get_or_404(id)
    novo = request.form.get('status')
    if novo in ('aberto', 'pago', 'ignorado'):
        conta.status = novo
        if novo != 'pago':
            conta.pago_em = None
            conta.valor_pago = 0
        conta.editado_em = agora()
        conta.editado_por_id = current_user.id
        db.session.commit()
        flash(f'Status alterado para {novo}.', 'success')
    return redirect(url_for('contas_pagar.detalhe', id=id))

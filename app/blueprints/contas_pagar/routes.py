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
from app.models import (
    ContaPagar,
    Fornecedor,
)
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


def _mapa_lojas_nf():
    """OrderedDict {canal_id: nome_loja} dos canais de NF. Nome vem do config
    SLACK_CANAIS_NF_NOMES; canais sem nome no config caem pro nome do canal
    Slack (e por fim o ID)."""
    from collections import OrderedDict

    mapa = OrderedDict()
    raw = (current_app.config.get('SLACK_CANAIS_NF_NOMES') or '').strip()
    for par in raw.split(';'):
        par = par.strip()
        if '=' in par:
            cid, nome = par.split('=', 1)
            if cid.strip():
                mapa[cid.strip()] = nome.strip()
    ids = (current_app.config.get('SLACK_CANAIS_NF') or '').strip()
    for cid in (c.strip() for c in ids.split(',') if c.strip()):
        if cid not in mapa:
            from app.services import slack as slack_api
            mapa[cid] = slack_api.nome_canal(cid)
    return mapa


def _nome_loja(canal_id, mapa_lojas):
    """Nome amigavel de um canal: config > nome do canal Slack > ID."""
    if not canal_id:
        return None
    if canal_id in mapa_lojas:
        return mapa_lojas[canal_id]
    from app.services import slack as slack_api
    return slack_api.nome_canal(canal_id)


@contas_pagar_bp.route('/')
@login_required
@admin_required
def lista():
    from sqlalchemy import func

    aba = request.args.get('aba', 'aberto')
    if aba not in {s for s, _, _ in ABAS}:
        aba = 'aberto'
    status_filtro = {s: st for s, _, st in ABAS}[aba]

    mapa_lojas = _mapa_lojas_nf()
    loja_sel = (request.args.get('loja') or '').strip()
    if loja_sel and loja_sel not in mapa_lojas:
        loja_sel = ''

    def _por_loja(q):
        return q.filter(ContaPagar.origem_canal == loja_sel) if loja_sel else q

    # Mostra so os "principais" (relacionado_id NULL). NF+boleto do mesmo
    # recebimento contam como UMA linha (o secundario aponta pro principal).
    def _principais(q):
        return q.filter(ContaPagar.relacionado_id.is_(None))

    cont = dict(_principais(_por_loja(db.session.query(ContaPagar.status, func.count())))
                .group_by(ContaPagar.status).all())
    contagens = {slug: cont.get(st, 0) for slug, _, st in ABAS}
    por_loja = dict(_principais(db.session.query(ContaPagar.origem_canal, func.count()))
                    .group_by(ContaPagar.origem_canal).all())

    contas = (_principais(_por_loja(
                  ContaPagar.query.filter(ContaPagar.status == status_filtro)))
              .order_by(ContaPagar.vencimento.is_(None),
                        ContaPagar.vencimento.asc(),
                        ContaPagar.criado_em.desc())
              .limit(200).all())
    # Documentos secundarios de cada grupo (pra mostrar "NF + boleto" na linha).
    ids = [c.id for c in contas]
    grupo_docs = {}
    if ids:
        for s in ContaPagar.query.filter(ContaPagar.relacionado_id.in_(ids)).all():
            grupo_docs.setdefault(s.relacionado_id, []).append(s)
    return render_template('contas_pagar/lista.html', contas=contas,
                           abas=ABAS, aba_atual=aba, contagens=contagens,
                           mapa_lojas=mapa_lojas, loja_sel=loja_sel,
                           total_por_loja=por_loja, grupo_docs=grupo_docs)


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
    mapa_lojas = _mapa_lojas_nf()
    return render_template('contas_pagar/detalhe.html', conta=conta, itens=itens,
                           fornecedores=fornecedores, relacionaveis=relacionaveis,
                           loja_nome=_nome_loja(conta.origem_canal, mapa_lojas))


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


@contas_pagar_bp.route('/<int:id>/reextrair', methods=['POST'])
@login_required
@admin_required
def reextrair(id):
    """Re-le o documento com a IA (rebaixa a imagem do Dropbox). Sobrescreve
    os campos de leitura; preserva decisoes humanas (status, pago, vinculo,
    fornecedor cadastrado). Util pra corrigir leitura errada (ex: data)."""
    import requests

    from app.services import conta_pagar_ia, conta_pagar_slack

    conta = ContaPagar.query.get_or_404(id)
    if not conta.imagem_url:
        flash('Sem imagem pra reprocessar.', 'warning')
        return redirect(url_for('contas_pagar.detalhe', id=id))
    try:
        resp = requests.get(conta.imagem_url, timeout=30)
        resp.raise_for_status()
    except Exception:
        flash('Nao consegui baixar a imagem do Dropbox.', 'danger')
        return redirect(url_for('contas_pagar.detalhe', id=id))

    dados = conta_pagar_ia.extrair_documento(
        resp.content, resp.headers.get('Content-Type') or 'image/jpeg')
    if dados.get('erro'):
        flash(f"IA nao conseguiu reler: {dados['erro']}", 'warning')
        return redirect(url_for('contas_pagar.detalhe', id=id))

    conta.tipo_documento = dados.get('tipo_documento') or conta.tipo_documento
    conta.fornecedor_nome = dados.get('fornecedor') or conta.fornecedor_nome
    if dados.get('valor_total') is not None:
        conta.valor_total = dados['valor_total']
    venc = conta_pagar_slack._parse_vencimento(dados)
    if venc:
        conta.vencimento = venc
    if dados.get('nf_numero'):
        conta.nf_numero = str(dados['nf_numero'])
    conta.codigo_barras = dados.get('codigo_barras') or conta.codigo_barras
    conta.linha_digitavel = dados.get('linha_digitavel') or conta.linha_digitavel
    conta.info_pagamento = dados.get('info_pagamento') or conta.info_pagamento
    if dados.get('itens'):
        conta.itens_json = json.dumps(dados['itens'], ensure_ascii=False)
    conta.dados_ia_json = json.dumps(dados, ensure_ascii=False)[:8000]
    conta.editado_em = agora()
    conta.editado_por_id = current_user.id
    db.session.commit()
    flash('Documento relido pela IA. Confira os campos, principalmente o '
          'vencimento.', 'success')
    return redirect(url_for('contas_pagar.detalhe', id=id))


@contas_pagar_bp.route('/<int:id>/pagar', methods=['POST'])
@login_required
@admin_required
def pagar(id):
    conta = ContaPagar.query.get_or_404(id)
    forma = (request.form.get('forma_pagamento') or '').strip() or None
    agora_ts = agora()
    # Marca o grupo inteiro (NF + boleto sao a mesma obrigacao).
    for alvo in [conta, *conta.ligados]:
        alvo.status = 'pago'
        alvo.valor_pago = alvo.valor_total
        alvo.pago_em = agora_ts
        alvo.forma_pagamento = forma
        alvo.editado_em = agora_ts
        alvo.editado_por_id = current_user.id
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
        agora_ts = agora()
        for alvo in [conta, *conta.ligados]:
            alvo.status = novo
            if novo != 'pago':
                alvo.pago_em = None
                alvo.valor_pago = 0
            alvo.editado_em = agora_ts
            alvo.editado_por_id = current_user.id
        db.session.commit()
        flash(f'Status alterado para {novo}.', 'success')
    return redirect(url_for('contas_pagar.detalhe', id=id))


@contas_pagar_bp.route('/juntar-automatico', methods=['POST'])
@login_required
@admin_required
def juntar_automatico():
    """Junta NF + boleto do mesmo recebimento (mesma loja, valor e
    vencimento). Retroativo e idempotente."""
    from app.services import conta_pagar as cp_dominio
    n = cp_dominio.agrupar_automatico()
    if n:
        flash(f'{n} documento(s) agrupado(s) ao seu par (NF ↔ boleto).', 'success')
    else:
        flash('Nenhum novo par encontrado pra juntar.', 'info')
    return redirect(url_for('contas_pagar.lista'))

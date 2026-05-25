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
    ContaPagarItemMap,
    Fornecedor,
    Loja,
    MateriaPrima,
    VariacaoPrecoMP,
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
    """OrderedDict {canal_id: nome_loja} dos canais de NF. Prioriza o vinculo
    confirmado na tela Canais -> Loja (SlackCanalLojaMap); senao o nome do
    config SLACK_CANAIS_NF_NOMES; senao o nome do canal no Slack (e por fim o ID)."""
    from collections import OrderedDict

    from app.models import SlackCanalLojaMap

    cfg = {}
    raw = (current_app.config.get('SLACK_CANAIS_NF_NOMES') or '').strip()
    for par in raw.split(';'):
        par = par.strip()
        if '=' in par:
            cid, nome = par.split('=', 1)
            if cid.strip():
                cfg[cid.strip()] = nome.strip()

    vinc = {}
    for m in SlackCanalLojaMap.query.all():
        if m.eh_industria:
            vinc[m.canal_id] = 'Indústria'
        elif m.loja_id and m.loja:
            vinc[m.canal_id] = m.loja.nome

    mapa = OrderedDict()
    ids = (current_app.config.get('SLACK_CANAIS_NF') or '').strip()
    canais = [c.strip() for c in ids.split(',') if c.strip()]
    for cid in cfg:                  # canais que so estao no config tambem entram
        if cid not in canais:
            canais.append(cid)
    for cid in canais:
        if cid in vinc:
            mapa[cid] = vinc[cid]
        elif cid in cfg:
            mapa[cid] = cfg[cid]
        else:
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
    from app.services.conta_pagar_estoque import normalizar_item_nome, sugerir_para_item
    conta = ContaPagar.query.get_or_404(id)
    itens = []
    if conta.itens_json:
        try:
            itens = json.loads(conta.itens_json)
        except (json.JSONDecodeError, TypeError):
            itens = []
    # Anexa o vinculo (ContaPagarItemMap por nome) a cada item, pra vincular a
    # MP direto aqui, vendo a nota. Sugestao da IA so pros ainda nao mapeados.
    norms = [normalizar_item_nome(it.get('nome') or '') for it in itens]
    mapas = {}
    presentes = [n for n in norms if n]
    if presentes:
        for m in ContaPagarItemMap.query.filter(
                ContaPagarItemMap.item_nome_norm.in_(presentes)).all():
            mapas[m.item_nome_norm] = m
    itens_vinc = []
    for i, (it, n) in enumerate(zip(itens, norms)):
        mp_map = mapas.get(n)
        sug = sugerir_para_item(it.get('nome') or '')[:3] if (n and not mp_map) else []
        itens_vinc.append({'indice': i, 'item': it, 'mapa': mp_map, 'sugestoes': sug})
    mps = MateriaPrima.query.order_by(MateriaPrima.nome).all()
    fornecedores = Fornecedor.query.filter_by(ativo=True).order_by(Fornecedor.nome).all()
    # Candidatos pra vincular (mesmo... outro documento ainda nao relacionado)
    relacionaveis = (ContaPagar.query
                     .filter(ContaPagar.id != conta.id)
                     .filter(ContaPagar.status != 'ignorado')
                     .order_by(ContaPagar.criado_em.desc())
                     .limit(50).all())
    mapa_lojas = _mapa_lojas_nf()
    return render_template('contas_pagar/detalhe.html', conta=conta, itens=itens,
                           itens_vinc=itens_vinc, mps=mps,
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
    background (a IA por imagem demora). As contas aparecem aos poucos.

    Com `canal_id` no form, importa so aquele canal (botao por canal na tela
    /canais); sem, importa todos os canais configurados."""
    import threading

    from app.services import conta_pagar_slack

    app_obj = current_app._get_current_object()
    canal_id = (request.form.get('canal_id') or '').strip() or None
    canais = [canal_id] if canal_id else None

    def _runner():
        try:
            conta_pagar_slack.importar_historico(app_obj, dias=30, canais=canais)
        except Exception:
            app_obj.logger.exception('importar_historico falhou')

    threading.Thread(target=_runner, daemon=True).start()
    alvo = 'deste canal' if canal_id else 'de todos os canais'
    flash(f'Importacao do historico (30 dias) {alvo} iniciada. As contas vao '
          'aparecer conforme forem processadas.', 'info')
    return redirect(url_for('contas_pagar.canais' if canal_id else 'contas_pagar.lista'))


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


def _parse_fator(raw):
    """Fator de conversao do form (aceita virgula BR). Retorna float ou None."""
    if raw is None or str(raw).strip() == '':
        return None
    s = str(raw).strip().replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return None


def _aplicar_acao_mapa(m):
    """Aplica a acao do form (vincular/ignorar/desfazer) num ContaPagarItemMap.
    Compartilhado entre a tela de mapeamentos e o vinculo no detalhe da conta."""
    acao = request.form.get('acao')
    if acao == 'ignorar':
        m.ignorar = True
        m.materia_prima_id = None
        m.confirmado_em = None
    elif acao == 'desfazer':
        m.materia_prima_id = None
        m.confirmado_em = None
        m.confirmado_por = None
        m.ignorar = False
    else:  # vincular
        mid = request.form.get('materia_prima_id')
        m.materia_prima_id = int(mid) if mid and mid.isdigit() else None
        m.unidade_compra = (request.form.get('unidade_compra') or '').strip() or None
        fator = _parse_fator(request.form.get('fator_conversao'))
        m.fator_conversao = fator if (fator and fator > 0) else 1.0
        m.ignorar = False
        if m.materia_prima_id:
            m.confirmado_em = agora()
            m.confirmado_por = current_user.id
        else:
            m.confirmado_em = None


# ── Vinculo canal -> loja (cada canal = 1 empresa = 1 estoque) ──

@contas_pagar_bp.route('/canais')
@login_required
@admin_required
def canais():
    from app.services.conta_pagar_estoque import normalizar_item_nome, resolver_canal_map
    mapa_lojas = _mapa_lojas_nf()
    linhas = []
    for cid, nome in mapa_lojas.items():
        m = resolver_canal_map(cid)
        linhas.append({'canal_id': cid, 'nome_canal': nome, 'mapa': m})
    db.session.commit()  # persiste mapas criados (auto-fuzzy) na 1a visita
    # Industria nao entra na lista: e escolhida pela opcao dedicada do seletor
    # (estoque global de producao), nunca como uma EstoqueLoja.
    lojas = [lj for lj in Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
             if normalizar_item_nome(lj.nome) != 'industria']
    return render_template('contas_pagar/canais.html', linhas=linhas, lojas=lojas)


@contas_pagar_bp.route('/canais/<canal_id>', methods=['POST'])
@login_required
@admin_required
def canal_vincular(canal_id):
    from app.services.conta_pagar_estoque import normalizar_item_nome, resolver_canal_map
    m = resolver_canal_map(canal_id)
    acao = request.form.get('acao')
    if acao == 'ignorar':
        m.ignorar = True
        m.confirmado_em = None
    elif acao == 'desfazer':
        m.loja_id = None
        m.eh_industria = False
        m.ignorar = False
        m.confirmado_em = None
        m.confirmado_por = None
    else:  # vincular — seletor unico: 'ind' (estoque global) ou id de loja
        destino = (request.form.get('destino') or '').strip()
        if destino == 'ind':
            m.eh_industria = True
            m.loja_id = None
        elif destino.isdigit():
            m.loja_id = int(destino)
            loja = db.session.get(Loja, m.loja_id)
            # Loja "Industria" sempre roteia pro estoque global, nunca EstoqueLoja.
            m.eh_industria = bool(loja and normalizar_item_nome(loja.nome) == 'industria')
            if m.eh_industria:
                m.loja_id = None
        else:
            m.eh_industria = False
            m.loja_id = None
        m.ignorar = False
        m.auto_match = False
        if m.loja_id or m.eh_industria:
            m.confirmado_em = agora()
            m.confirmado_por = current_user.id
        else:
            m.confirmado_em = None
    db.session.commit()
    flash('Vinculo do canal atualizado.', 'success')
    return redirect(url_for('contas_pagar.canais'))


# ── Mapeamento item de NF -> materia-prima ──

def _exemplos_itens_nf():
    """{nome_norm: {quantidade, valor_unitario, valor_total, unidade, n_notas}}
    dos itens_json de todas as contas — pra mostrar na tela de mapeamentos os
    dados que vieram da nota (ajuda a bater o fator). Exemplo = nota mais recente."""
    from app.services.conta_pagar_estoque import normalizar_item_nome
    ex = {}
    contas = (ContaPagar.query
              .filter(ContaPagar.itens_json.isnot(None))
              .order_by(ContaPagar.criado_em.desc()).all())
    for c in contas:
        try:
            itens = json.loads(c.itens_json or '[]')
        except (json.JSONDecodeError, TypeError):
            continue
        for it in itens:
            if not isinstance(it, dict):
                continue
            norm = normalizar_item_nome(it.get('nome') or '')
            if not norm:
                continue
            if norm not in ex:
                ex[norm] = {'quantidade': it.get('quantidade'),
                            'valor_unitario': it.get('valor_unitario'),
                            'valor_total': it.get('valor_total'),
                            'unidade': it.get('unidade'), 'n_notas': 0}
            ex[norm]['n_notas'] += 1
    return ex


@contas_pagar_bp.route('/mapeamentos')
@login_required
@admin_required
def mapeamentos():
    from app.services.conta_pagar_estoque import sugerir_para_item
    estado = request.args.get('estado', 'pendente')
    todos = ContaPagarItemMap.query.order_by(ContaPagarItemMap.item_nome_exemplo).all()
    contagens = {'pendente': 0, 'mapeado': 0, 'ignorado': 0}
    for m in todos:
        contagens[m.estado] = contagens.get(m.estado, 0) + 1
    maps = [m for m in todos if m.estado == estado] if estado in contagens else todos
    sugestoes = {}
    for m in maps:
        if m.estado == 'pendente':
            sugestoes[m.id] = sugerir_para_item(m.item_nome_exemplo)[:3]
    mps = MateriaPrima.query.order_by(MateriaPrima.nome).all()
    exemplos = _exemplos_itens_nf()
    return render_template('contas_pagar/mapeamentos.html', maps=maps, mps=mps,
                           estado=estado, contagens=contagens, sugestoes=sugestoes,
                           exemplos=exemplos)


@contas_pagar_bp.route('/mapeamentos/limpar-nomes', methods=['POST'])
@login_required
@owner_required
def mapeamentos_limpar_nomes():
    """Re-normaliza os nomes dos itens (ignora validade/lote) e junta os
    vinculos duplicados. Preserva os confirmados."""
    from app.services.conta_pagar_estoque import migrar_nomes_itens
    stats = migrar_nomes_itens()
    msg = (f"{stats['mesclados']} duplicado(s) juntado(s), "
           f"{stats['atualizados']} nome(s) limpo(s).")
    if stats['conflitos']:
        msg += (f" {stats['conflitos']} grupo(s) com vinculos divergentes "
                "ficaram sem mesclar (revise).")
    flash(msg, 'success')
    return redirect(url_for('contas_pagar.mapeamentos'))


@contas_pagar_bp.route('/mapeamentos/<int:id>', methods=['POST'])
@login_required
@admin_required
def mapeamento_vincular(id):
    m = ContaPagarItemMap.query.get_or_404(id)
    _aplicar_acao_mapa(m)
    db.session.commit()
    flash('Mapeamento atualizado.', 'success')
    return redirect(url_for('contas_pagar.mapeamentos',
                            estado=request.form.get('estado') or 'pendente'))


@contas_pagar_bp.route('/<int:id>/item/<int:indice>/vincular', methods=['POST'])
@login_required
@admin_required
def item_vincular(id, indice):
    """Vincula/ignora um item de NF a uma MP direto da tela de detalhe da conta
    (vendo a nota). Cria o ContaPagarItemMap por nome se ainda nao existir; o
    vinculo vale pra todas as NFs com o mesmo nome de item."""
    from app.services.conta_pagar_estoque import normalizar_item_nome
    conta = ContaPagar.query.get_or_404(id)
    try:
        itens = json.loads(conta.itens_json or '[]')
    except (json.JSONDecodeError, TypeError):
        itens = []
    if not (0 <= indice < len(itens)):
        flash('Item nao encontrado.', 'warning')
        return redirect(url_for('contas_pagar.detalhe', id=id))
    nome = (itens[indice].get('nome') or '').strip()
    norm = normalizar_item_nome(nome)
    if not norm:
        flash('Item sem nome — nao da pra vincular.', 'warning')
        return redirect(url_for('contas_pagar.detalhe', id=id))
    m = ContaPagarItemMap.query.filter_by(item_nome_norm=norm).first()
    if not m:
        m = ContaPagarItemMap(item_nome_norm=norm, item_nome_exemplo=nome)
        db.session.add(m)
    _aplicar_acao_mapa(m)
    db.session.commit()
    flash('Item atualizado.', 'success')
    return redirect(url_for('contas_pagar.detalhe', id=id))


# ── Avisos de variacao de preco ──

@contas_pagar_bp.route('/variacoes')
@login_required
@admin_required
def variacoes():
    from sqlalchemy import func
    filtro = request.args.get('f', 'todos')
    q = VariacaoPrecoMP.query.filter_by(status='novo')
    if filtro == 'subiu':
        q = q.filter(VariacaoPrecoMP.variacao_pct > 0)
    elif filtro == 'caiu':
        q = q.filter(VariacaoPrecoMP.variacao_pct < 0)
    itens = q.order_by(func.abs(VariacaoPrecoMP.variacao_pct).desc()).limit(200).all()
    novos = VariacaoPrecoMP.query.filter_by(status='novo').count()
    return render_template('contas_pagar/variacoes.html', itens=itens,
                           filtro=filtro, novos=novos)


@contas_pagar_bp.route('/variacoes/<int:id>', methods=['POST'])
@login_required
@admin_required
def variacao_acao(id):
    v = VariacaoPrecoMP.query.get_or_404(id)
    acao = request.form.get('acao')
    if acao in ('aprovar', 'ignorar'):
        v.status = 'aprovado' if acao == 'aprovar' else 'ignorado'
        v.revisado_em = agora()
        v.revisado_por_id = current_user.id
        db.session.commit()
        flash('Variacao revisada.', 'success')
    return redirect(url_for('contas_pagar.variacoes', f=request.form.get('f') or 'todos'))

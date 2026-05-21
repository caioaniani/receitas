"""B2B — venda da industria pra clientes externos.

Rotas:
- /b2b — dashboard (vendas recentes + contas a receber)
- /b2b/clientes — CRUD cliente B2B
- /b2b/precos — tabela de preco atacado
- /b2b/vendas/nova — formulario nova venda
- /b2b/vendas/<id> — detalhe + receber pagamento + cancelar
- /b2b/contas-a-receber — parcelas em aberto

Baixa do estoque ocorre em vendas_b2b.criar_venda (service).
Cancelamento estorna automaticamente.
"""
from datetime import date

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload

from app.blueprints.b2b import b2b_bp
from app.decorators import admin_required
from app.extensions import db
from app.models import ClienteB2B, EstoqueProducao, Produto, Receita, VendaB2B, VendaB2BParcela
from app.services import vendas_b2b as svc
from app.utils import hoje

# ── Dashboard ──

@b2b_bp.route('/')
@login_required
@admin_required
def dashboard():
    vendas_recentes = (VendaB2B.query
                       .options(joinedload(VendaB2B.cliente),
                                joinedload(VendaB2B.parcelas))
                       .order_by(VendaB2B.data_venda.desc(),
                                 VendaB2B.id.desc())
                       .limit(20).all())
    abertas = (VendaB2BParcela.query
               .join(VendaB2B)
               .filter(VendaB2B.status == 'ativa',
                       VendaB2BParcela.pago_em.is_(None))
               .order_by(VendaB2BParcela.vencimento.asc())
               .limit(30).all())
    return render_template('b2b/dashboard.html',
                           vendas_recentes=vendas_recentes,
                           parcelas_abertas=abertas)


# ── Clientes ──

@b2b_bp.route('/clientes')
@login_required
@admin_required
def clientes():
    cs = ClienteB2B.query.order_by(ClienteB2B.ativo.desc(),
                                    ClienteB2B.nome.asc()).all()
    return render_template('b2b/clientes.html', clientes=cs)


@b2b_bp.route('/clientes/novo', methods=['POST'])
@login_required
@admin_required
def cliente_novo():
    nome = (request.form.get('nome') or '').strip()
    if not nome:
        flash('Nome obrigatorio.', 'danger')
        return redirect(url_for('b2b.clientes'))
    if ClienteB2B.query.filter_by(nome=nome).first():
        flash(f'Cliente "{nome}" ja existe.', 'warning')
        return redirect(url_for('b2b.clientes'))
    c = ClienteB2B(
        nome=nome,
        cnpj_cpf=(request.form.get('cnpj_cpf') or '').strip() or None,
        telefone=(request.form.get('telefone') or '').strip() or None,
        email=(request.form.get('email') or '').strip() or None,
        endereco=(request.form.get('endereco') or '').strip() or None,
        contato=(request.form.get('contato') or '').strip() or None,
        desconto_percentual=float(request.form.get('desconto_percentual') or 0),
        observacao=(request.form.get('observacao') or '').strip() or None,
    )
    db.session.add(c)
    db.session.commit()
    flash(f'Cliente "{nome}" cadastrado.', 'success')
    return redirect(url_for('b2b.clientes'))


@b2b_bp.route('/clientes/<int:cid>/editar', methods=['POST'])
@login_required
@admin_required
def cliente_editar(cid):
    c = ClienteB2B.query.get_or_404(cid)
    c.cnpj_cpf = (request.form.get('cnpj_cpf') or '').strip() or None
    c.telefone = (request.form.get('telefone') or '').strip() or None
    c.email = (request.form.get('email') or '').strip() or None
    c.endereco = (request.form.get('endereco') or '').strip() or None
    c.contato = (request.form.get('contato') or '').strip() or None
    c.desconto_percentual = float(request.form.get('desconto_percentual') or 0)
    c.observacao = (request.form.get('observacao') or '').strip() or None
    c.ativo = bool(request.form.get('ativo'))
    db.session.commit()
    flash(f'{c.nome} atualizado.', 'success')
    return redirect(url_for('b2b.clientes'))


# ── Vendas ──

@b2b_bp.route('/vendas')
@login_required
@admin_required
def vendas():
    q = (VendaB2B.query
         .options(joinedload(VendaB2B.cliente))
         .order_by(VendaB2B.data_venda.desc(), VendaB2B.id.desc()))
    status = request.args.get('status')
    if status in ('ativa', 'cancelada'):
        q = q.filter_by(status=status)
    return render_template('b2b/vendas.html', vendas=q.limit(100).all(),
                           status_filtro=status)


@b2b_bp.route('/vendas/nova')
@login_required
@admin_required
def venda_nova():
    clientes = ClienteB2B.query.filter_by(ativo=True).order_by(ClienteB2B.nome).all()
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    produtos = Produto.query.filter_by(ativo=True).order_by(Produto.nome).all()
    # Preco atacado vem do cadastro: Receita.preco_venda, Produto.preco_atacado
    # (mesma logica de /cardapio?tipo=atacado).
    precos_map = {}
    for r in receitas:
        if r.preco_venda:
            precos_map[f'receita:{r.id}'] = r.preco_venda
    for p in produtos:
        if p.preco_atacado:
            precos_map[f'produto:{p.id}'] = p.preco_atacado
    # Estoque atual por item (pra UI mostrar saldo)
    estoque_map = {}
    for ep in EstoqueProducao.query.all():
        if ep.receita_id:
            estoque_map[f'receita:{ep.receita_id}'] = ep.quantidade or 0
        elif ep.produto_id:
            estoque_map[f'produto:{ep.produto_id}'] = ep.quantidade or 0
    return render_template('b2b/venda_nova.html', clientes=clientes,
                           receitas=receitas, produtos=produtos,
                           precos_map=precos_map, estoque_map=estoque_map,
                           hoje=hoje().isoformat())


@b2b_bp.route('/vendas/nova', methods=['POST'])
@login_required
@admin_required
def venda_criar():
    cliente_id = request.form.get('cliente_id', type=int) or None
    cliente_nome = (request.form.get('cliente_nome') or '').strip() or None
    data_str = request.form.get('data_venda') or hoje().isoformat()
    try:
        data_venda = date.fromisoformat(data_str)
    except ValueError:
        data_venda = hoje()
    nf = (request.form.get('nf_numero') or '').strip() or None
    obs = (request.form.get('observacao') or '').strip() or None

    # Itens: nome no form item_ref[] = "receita:5", item_qtd[], item_preco[]
    refs = request.form.getlist('item_ref[]')
    qtds = request.form.getlist('item_qtd[]')
    precos = request.form.getlist('item_preco[]')
    descs = request.form.getlist('item_desc[]')
    itens = []
    for i, ref in enumerate(refs):
        ref = (ref or '').strip()
        if not ref or ':' not in ref:
            continue
        tipo, _, sid = ref.partition(':')
        if tipo not in ('receita', 'produto') or not sid.isdigit():
            continue
        try:
            qtd = int(qtds[i])
        except (IndexError, ValueError):
            continue
        if qtd <= 0:
            continue
        try:
            preco = float((precos[i] or '0').replace(',', '.'))
        except (IndexError, ValueError):
            preco = 0
        try:
            desc = float((descs[i] or '0').replace(',', '.'))
        except (IndexError, ValueError):
            desc = 0
        itens.append({'tipo': tipo, 'id': int(sid), 'quantidade': qtd,
                      'preco_unitario': preco, 'desconto_percentual': desc})
    if not itens:
        flash('Adicione pelo menos 1 item.', 'danger')
        return redirect(url_for('b2b.venda_nova'))

    # Parcelas: parcela_venc[], parcela_valor[], parcela_forma[]
    vencs = request.form.getlist('parcela_venc[]')
    valores = request.form.getlist('parcela_valor[]')
    formas = request.form.getlist('parcela_forma[]')
    parcelas = []
    for i, v in enumerate(vencs):
        v = (v or '').strip()
        if not v:
            continue
        try:
            valor = float((valores[i] or '0').replace(',', '.'))
        except (IndexError, ValueError):
            valor = 0
        if valor <= 0:
            continue
        try:
            venc = date.fromisoformat(v)
        except ValueError:
            continue
        forma = (formas[i] if i < len(formas) else '') or None
        parcelas.append({'vencimento': venc, 'valor': valor,
                         'forma_pagamento': forma})

    try:
        venda = svc.criar_venda(
            cliente_id=cliente_id, cliente_nome=cliente_nome,
            data_venda=data_venda, itens=itens,
            parcelas=parcelas or None, observacao=obs, nf_numero=nf,
            user=current_user,
        )
    except ValueError as exc:
        db.session.rollback()
        flash(f'Erro: {exc}', 'danger')
        return redirect(url_for('b2b.venda_nova'))

    flash(f'Venda B2B #{venda.id} criada — R$ {venda.valor_total:.2f}.', 'success')
    return redirect(url_for('b2b.venda_detalhe', vid=venda.id))


@b2b_bp.route('/vendas/<int:vid>')
@login_required
@admin_required
def venda_detalhe(vid):
    venda = (VendaB2B.query
             .options(joinedload(VendaB2B.cliente),
                      joinedload(VendaB2B.itens),
                      joinedload(VendaB2B.parcelas))
             .get_or_404(vid))
    return render_template('b2b/venda_detalhe.html', venda=venda)


@b2b_bp.route('/vendas/<int:vid>/cancelar', methods=['POST'])
@login_required
@admin_required
def venda_cancelar(vid):
    venda = VendaB2B.query.get_or_404(vid)
    svc.cancelar_venda(venda, user=current_user)
    flash(f'Venda #{vid} cancelada e estoque estornado.', 'warning')
    return redirect(url_for('b2b.venda_detalhe', vid=vid))


@b2b_bp.route('/parcelas/<int:pid>/receber', methods=['POST'])
@login_required
@admin_required
def parcela_receber(pid):
    p = VendaB2BParcela.query.get_or_404(pid)
    try:
        v = float((request.form.get('valor') or '0').replace(',', '.'))
    except ValueError:
        flash('Valor invalido.', 'danger')
        return redirect(url_for('b2b.venda_detalhe', vid=p.venda_id))
    forma = (request.form.get('forma_pagamento') or '').strip() or None
    obs = (request.form.get('observacao') or '').strip() or None
    try:
        svc.receber_pagamento(p, v, forma_pagamento=forma, observacao=obs)
    except ValueError as exc:
        flash(f'Erro: {exc}', 'danger')
        return redirect(url_for('b2b.venda_detalhe', vid=p.venda_id))
    flash(f'Pagamento de R$ {v:.2f} registrado.', 'success')
    return redirect(url_for('b2b.venda_detalhe', vid=p.venda_id))


# ── Contas a receber ──

@b2b_bp.route('/contas-a-receber')
@login_required
@admin_required
def contas_receber():
    parcelas = (VendaB2BParcela.query
                .join(VendaB2B)
                .options(joinedload(VendaB2BParcela.venda)
                         .joinedload(VendaB2B.cliente))
                .filter(VendaB2B.status == 'ativa',
                        VendaB2BParcela.pago_em.is_(None))
                .order_by(VendaB2BParcela.vencimento.asc())
                .all())
    total_aberto = sum(p.saldo for p in parcelas)
    return render_template('b2b/contas_receber.html', parcelas=parcelas,
                           total_aberto=total_aberto, hoje=hoje())

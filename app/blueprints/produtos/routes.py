from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import login_required

from app.blueprints.produtos import produtos_bp
from app.decorators import catalogo_required
from app.extensions import db
from app.models import MateriaPrima, Produto, ProdutoItem, Receita
from app.services.custos import calcular_custo_produto, calcular_custos_receitas
from app.utils import parse_float_br


@produtos_bp.route('/')
@login_required
def lista():
    produtos = Produto.query.order_by(Produto.categoria, Produto.nome).all()
    resultado = calcular_custos_receitas()
    fabricados = resultado['fabricados']

    # Calcular custo de cada cesta — passa dict de produto_custos pra
    # resolver componentes tipo='produto' (cesta-de-cesta).
    from app.services.custos import calcular_custos_produtos
    produto_custos_idx = calcular_custos_produtos(resultado['custos'],
                                                    resultado['mp_info'])
    cestas = []
    for p in produtos:
        custo = calcular_custo_produto(p, resultado['custos'],
                                        resultado['mp_info'],
                                        produto_custos_idx)
        cestas.append({
            'id': p.id,
            'nome': p.nome,
            'categoria': p.categoria or '',
            'descricao': p.descricao or '',
            'num_itens': len(p.itens),
            'custo': custo,
            'preco_atacado': p.preco_atacado or 0,
            'preco_loja': p.preco_loja or 0,
            'preco_site': p.preco_site or 0,
            'observacao': p.observacao or '',
        })

    return render_template('produtos/lista.html', fabricados=fabricados, cestas=cestas)


@produtos_bp.route('/novo', methods=['POST'])
@login_required
@catalogo_required
def novo():
    produto = Produto(nome='Nova Cesta', categoria='Cestas')
    db.session.add(produto)
    db.session.commit()
    return redirect(url_for('produtos.detalhe', id=produto.id))


@produtos_bp.route('/<int:id>')
@login_required
def detalhe(id):
    produto = Produto.query.get_or_404(id)
    resultado = calcular_custos_receitas()
    receita_custos = resultado['custos']
    mp_info = resultado['mp_info']

    # Indice de custo de cada Produto. Considera composicao: se o produto
    # tem ProdutoItens (cesta), soma componentes; se nao tem, usa custo_direto.
    # Suporta cesta-dentro-de-cesta via iteracao.
    from app.services.custos import calcular_custos_produtos
    produto_custos = calcular_custos_produtos(receita_custos, mp_info)

    # Lookups normalizados (case/espaco-tolerant) pra evitar custo zero
    # quando grafia do item_nome divergir do cadastro (ex: "Iogurte 200ml"
    # vs "iogurte 200ml ").
    def _norm(s):
        return (s or '').strip().casefold()
    receita_custos_n = {_norm(k): v for k, v in receita_custos.items()}
    produto_custos_n = {_norm(k): v for k, v in produto_custos.items()}
    mp_info_n = {_norm(k): v for k, v in mp_info.items()}

    # Custo de cada item para exibir no template
    itens_data = []
    for item in produto.itens:
        info = {}
        if item.tipo == 'receita':
            custo_un = receita_custos.get(item.item_nome)
            if custo_un is None:
                custo_un = receita_custos_n.get(_norm(item.item_nome), 0)
            unidade = 'un'
        elif item.tipo == 'produto':
            custo_un = produto_custos.get(item.item_nome)
            if custo_un is None:
                custo_un = produto_custos_n.get(_norm(item.item_nome), 0)
            unidade = 'un'
        else:
            info = mp_info.get(item.item_nome) or mp_info_n.get(_norm(item.item_nome), {})
            custo_kg = info.get('custo_por_kg', 0)
            unidade = info.get('unidade', 'un')
            if unidade in ('g', 'ml'):
                custo_un = custo_kg / 1000
            else:
                custo_un = custo_kg
        itens_data.append({
            'tipo': item.tipo,
            'item_nome': item.item_nome,
            'quantidade': item.quantidade,
            'custo_un': custo_un,
            'unidade': unidade,
            'custo_por_kg': info.get('custo_por_kg', 0) if item.tipo == 'mp' else None,
        })

    custo_total = sum(i['custo_un'] * i['quantidade'] for i in itens_data)

    return render_template('produtos/detalhe.html',
                           produto=produto,
                           itens_data=itens_data,
                           custo_total=custo_total,
                           receita_custos=receita_custos,
                           produto_custos=produto_custos)


@produtos_bp.route('/<int:id>/salvar', methods=['POST'])
@login_required
@catalogo_required
def salvar_composicao(id):
    produto = Produto.query.get_or_404(id)

    produto.nome = request.form.get('nome', '').strip() or produto.nome
    produto.categoria = request.form.get('categoria', '').strip() or None
    produto.descricao = request.form.get('descricao', '').strip() or None
    produto.imagem_url = request.form.get('imagem_url', '').strip() or None

    produto.preco_atacado = parse_float_br(request.form.get('preco_atacado', ''))
    produto.preco_loja = parse_float_br(request.form.get('preco_loja', ''))
    produto.preco_site = parse_float_br(request.form.get('preco_site', ''))
    produto.custo_direto = parse_float_br(request.form.get('custo_direto', ''))
    produto.custo_embalagem = parse_float_br(request.form.get('custo_embalagem', ''), default=0)
    produto.modo_preparo = request.form.get('modo_preparo', '').strip() or None
    produto.observacao = request.form.get('observacao', '').strip() or None

    # Recriar itens
    ProdutoItem.query.filter_by(produto_id=produto.id).delete()

    tipos = request.form.getlist('item_tipo[]')
    nomes = request.form.getlist('item_nome[]')
    qtds = request.form.getlist('quantidade[]')

    for i in range(len(nomes)):
        nome = nomes[i].strip()
        if not nome:
            continue
        tipo = tipos[i] if i < len(tipos) else 'receita'
        qtd_str = qtds[i].replace(',', '.').strip() if i < len(qtds) else '1'
        qtd = float(qtd_str) if qtd_str else 1

        # Resolve FK por nome exato — se nao bater, item fica orfao
        # e admin precisa vincular em /produtos/cestas/orfaos.
        receita_id = None
        produto_componente_id = None
        materia_prima_id = None
        if tipo == 'receita':
            r = Receita.query.filter_by(nome=nome).first()
            receita_id = r.id if r else None
        elif tipo == 'produto':
            p = Produto.query.filter_by(nome=nome).first()
            # Nao deixa cesta apontar pra ela mesma (loop infinito).
            if p and p.id != produto.id:
                produto_componente_id = p.id
        elif tipo == 'mp':
            m = MateriaPrima.query.filter_by(nome=nome).first()
            materia_prima_id = m.id if m else None

        item = ProdutoItem(
            produto_id=produto.id,
            tipo=tipo,
            item_nome=nome,
            receita_id=receita_id,
            produto_componente_id=produto_componente_id,
            materia_prima_id=materia_prima_id,
            quantidade=qtd,
        )
        db.session.add(item)

    db.session.commit()
    flash(f'"{produto.nome}" salvo com sucesso!', 'success')
    return redirect(url_for('produtos.detalhe', id=produto.id))


@produtos_bp.route('/api/nova-mp', methods=['POST'])
@login_required
@catalogo_required
def nova_mp():
    """Cria matéria-prima via AJAX (sem sair da página da cesta)."""
    nome = request.form.get('mp_nome', '').strip()
    custo = request.form.get('mp_custo', '').replace(',', '.').strip()

    if not nome or not custo:
        return jsonify(success=False, error='Preencha nome e custo.')

    if MateriaPrima.query.filter_by(nome=nome).first():
        return jsonify(success=False, error=f'"{nome}" ja existe no banco de MP.')

    try:
        custo_float = float(custo)
    except ValueError:
        return jsonify(success=False, error='Custo invalido.')

    mp = MateriaPrima(nome=nome, unidade='un', custo_por_kg=custo_float)
    db.session.add(mp)
    db.session.commit()

    return jsonify(success=True, nome=nome, custo=custo_float)


@produtos_bp.route('/excluir/<int:id>', methods=['POST'])
@login_required
@catalogo_required
def excluir(id):
    produto = Produto.query.get_or_404(id)
    nome = produto.nome
    db.session.delete(produto)
    db.session.commit()
    flash(f'"{nome}" excluido!', 'success')
    return redirect(url_for('produtos.lista'))


@produtos_bp.route('/cestas')
@login_required
def cestas():
    """Lista produtos compostos (cestas) com diagnóstico de componentes.

    Cestas configuradas: produto com >=1 ProdutoItem. Aparece com a lista
    resumida dos componentes.
    Sem componentes: produto ativo sem nenhum ProdutoItem — pode ser uma
    cesta esquecida OU um produto simples. Admin decide.
    """
    from sqlalchemy.orm import joinedload
    produtos = (Produto.query
                .filter_by(ativo=True)
                .options(joinedload(Produto.itens))
                .order_by(Produto.categoria, Produto.nome).all())
    com_componentes = []
    sem_componentes = []
    for p in produtos:
        n = len(p.itens) if p.itens else 0
        if n > 0:
            com_componentes.append({'produto': p, 'n_componentes': n})
        else:
            sem_componentes.append({'produto': p})
    return render_template('produtos/cestas.html',
                            com_componentes=com_componentes,
                            sem_componentes=sem_componentes)


@produtos_bp.route('/cestas/orfaos')
@login_required
@catalogo_required
def cestas_orfaos():
    """Lista ProdutoItems sem FK vinculada (tipo definido mas receita_id /
    materia_prima_id NULL). Esses componentes NAO baixam estoque na venda.

    Owner ve isso destacado no dashboard. Admin pode vincular manualmente
    aqui — selecionar Receita/MP do dropdown ou marcar como `removido`
    (caso o componente nao deveria estar na cesta).
    """
    from sqlalchemy import or_
    orfaos = (ProdutoItem.query
              .filter(or_(
                  (ProdutoItem.tipo == 'receita') & (ProdutoItem.receita_id.is_(None)),
                  (ProdutoItem.tipo == 'produto') & (ProdutoItem.produto_componente_id.is_(None)),
                  (ProdutoItem.tipo == 'mp') & (ProdutoItem.materia_prima_id.is_(None)),
              ))
              .all())
    receitas = Receita.query.order_by(Receita.nome).all()
    produtos = Produto.query.filter(Produto.ativo.is_(True)).order_by(Produto.nome).all()
    mps = MateriaPrima.query.order_by(MateriaPrima.nome).all()
    return render_template('produtos/cestas_orfaos.html',
                            orfaos=orfaos, receitas=receitas,
                            produtos=produtos, mps=mps)


@produtos_bp.route('/cestas/orfaos/<int:id>/vincular', methods=['POST'])
@login_required
@catalogo_required
def vincular_orfao(id):
    """Vincula um ProdutoItem orfao a uma Receita, Produto ou MateriaPrima."""
    pi = ProdutoItem.query.get_or_404(id)
    alvo = (request.form.get('alvo') or '').strip()
    if not alvo or ':' not in alvo:
        flash('Selecione um item.', 'warning')
        return redirect(url_for('produtos.cestas_orfaos'))
    tipo, id_str = alvo.split(':', 1)
    try:
        target_id = int(id_str)
    except ValueError:
        flash('ID invalido.', 'warning')
        return redirect(url_for('produtos.cestas_orfaos'))

    if tipo == 'receita':
        r = Receita.query.get(target_id)
        if not r:
            flash('Receita nao encontrada.', 'warning')
            return redirect(url_for('produtos.cestas_orfaos'))
        pi.tipo = 'receita'
        pi.receita_id = r.id
        pi.produto_componente_id = None
        pi.materia_prima_id = None
        pi.item_nome = r.nome
    elif tipo == 'produto':
        p = Produto.query.get(target_id)
        if not p:
            flash('Produto nao encontrado.', 'warning')
            return redirect(url_for('produtos.cestas_orfaos'))
        if p.id == pi.produto_id:
            flash('Cesta nao pode conter ela mesma como componente.', 'warning')
            return redirect(url_for('produtos.cestas_orfaos'))
        pi.tipo = 'produto'
        pi.produto_componente_id = p.id
        pi.receita_id = None
        pi.materia_prima_id = None
        pi.item_nome = p.nome
    elif tipo == 'mp':
        m = MateriaPrima.query.get(target_id)
        if not m:
            flash('MP nao encontrada.', 'warning')
            return redirect(url_for('produtos.cestas_orfaos'))
        pi.tipo = 'mp'
        pi.materia_prima_id = m.id
        pi.receita_id = None
        pi.produto_componente_id = None
        pi.item_nome = m.nome
    else:
        flash('Tipo invalido.', 'warning')
        return redirect(url_for('produtos.cestas_orfaos'))

    db.session.commit()
    flash(f'Componente vinculado a "{pi.nome_resolvido}".', 'success')
    return redirect(url_for('produtos.cestas_orfaos'))


@produtos_bp.route('/cestas/orfaos/<int:id>/excluir', methods=['POST'])
@login_required
@catalogo_required
def excluir_orfao(id):
    """Remove um ProdutoItem orfao da cesta (caso o componente nao deveria
    estar la — ex: receita que foi deletada do catalogo)."""
    pi = ProdutoItem.query.get_or_404(id)
    nome = pi.item_nome
    db.session.delete(pi)
    db.session.commit()
    flash(f'Componente "{nome}" removido da cesta.', 'success')
    return redirect(url_for('produtos.cestas_orfaos'))

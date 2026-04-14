import json

from flask import render_template, redirect, url_for, flash, request, jsonify

from app.blueprints.produtos import produtos_bp
from app.extensions import db
from app.models import Produto, ProdutoItem, Receita, MateriaPrima


def _calcular_custos_receitas():
    """Calcula custo unitário de cada receita. Retorna dict {nome: custo_un} e lista fabricados."""
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    mp_dict = {mp.nome: mp.custo_por_kg for mp in MateriaPrima.query.all()}

    custos = {}
    fabricados = []

    for r in receitas:
        custo_total = 0
        sum_pct = 0
        for ing in r.ingredientes:
            sum_pct += ing.porcentagem
            qtd_g = r.peso_base * ing.porcentagem / 100
            custo_kg = mp_dict.get(ing.ingrediente_nome, 0)
            custo_total += qtd_g / 1000 * custo_kg

        total_qtd = r.peso_base * sum_pct / 100
        perda = r.perda_percentual or 0
        peso_pos_perda = total_qtd * (1 - perda / 100)

        if r.peso_unitario and r.peso_unitario > 0 and peso_pos_perda > 0:
            rendimento = int(peso_pos_perda / r.peso_unitario)
        else:
            rendimento = int(r.rendimento_qtd)

        embalagem = r.custo_embalagem or 0
        custo_un = (custo_total / rendimento + embalagem) if rendimento > 0 else 0
        custos[r.nome] = custo_un

        fabricados.append({
            'id': r.id,
            'nome': r.nome,
            'categoria': r.categoria or 'Outros',
            'peso_unitario': r.peso_unitario,
            'rendimento': rendimento,
            'custo_un': custo_un,
            'preco_atacado': r.preco_venda or 0,
            'preco_loja': r.preco_loja or 0,
            'preco_site': r.preco_site or 0,
        })

    return custos, fabricados, mp_dict


def _calcular_custo_cesta(produto, receita_custos, mp_dict):
    """Calcula custo total de uma cesta/produto."""
    if produto.itens:
        custo = 0
        for item in produto.itens:
            if item.tipo == 'receita':
                custo += (receita_custos.get(item.item_nome, 0)) * item.quantidade
            else:
                custo += (mp_dict.get(item.item_nome, 0)) * item.quantidade
        return custo
    elif produto.custo_direto:
        return produto.custo_direto
    return 0


@produtos_bp.route('/')
def lista():
    produtos = Produto.query.order_by(Produto.categoria, Produto.nome).all()
    receita_custos, fabricados, mp_dict = _calcular_custos_receitas()

    # Calcular custo de cada cesta
    cestas = []
    for p in produtos:
        custo = _calcular_custo_cesta(p, receita_custos, mp_dict)
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
        })

    return render_template('produtos/lista.html', fabricados=fabricados, cestas=cestas)


@produtos_bp.route('/novo', methods=['POST'])
def novo():
    produto = Produto(nome='Nova Cesta', categoria='Cestas')
    db.session.add(produto)
    db.session.commit()
    return redirect(url_for('produtos.detalhe', id=produto.id))


@produtos_bp.route('/<int:id>')
def detalhe(id):
    produto = Produto.query.get_or_404(id)
    receita_custos, _, mp_dict = _calcular_custos_receitas()

    # Custo de cada item para exibir no template
    itens_data = []
    for item in produto.itens:
        if item.tipo == 'receita':
            custo_un = receita_custos.get(item.item_nome, 0)
        else:
            custo_un = mp_dict.get(item.item_nome, 0)
        itens_data.append({
            'tipo': item.tipo,
            'item_nome': item.item_nome,
            'quantidade': item.quantidade,
            'custo_un': custo_un,
        })

    custo_total = sum(i['custo_un'] * i['quantidade'] for i in itens_data)
    receita_custos_json = json.dumps(receita_custos, ensure_ascii=False)

    return render_template('produtos/detalhe.html',
                           produto=produto,
                           itens_data=itens_data,
                           custo_total=custo_total,
                           receita_custos_json=receita_custos_json)


@produtos_bp.route('/<int:id>/salvar', methods=['POST'])
def salvar_composicao(id):
    produto = Produto.query.get_or_404(id)

    produto.nome = request.form.get('nome', '').strip() or produto.nome
    produto.categoria = request.form.get('categoria', '').strip() or None
    produto.descricao = request.form.get('descricao', '').strip() or None

    at = request.form.get('preco_atacado', '').replace(',', '.').strip()
    produto.preco_atacado = float(at) if at else None
    lj = request.form.get('preco_loja', '').replace(',', '.').strip()
    produto.preco_loja = float(lj) if lj else None
    st = request.form.get('preco_site', '').replace(',', '.').strip()
    produto.preco_site = float(st) if st else None
    cd = request.form.get('custo_direto', '').replace(',', '.').strip()
    produto.custo_direto = float(cd) if cd else None

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

        item = ProdutoItem(
            produto_id=produto.id,
            tipo=tipo,
            item_nome=nome,
            quantidade=qtd,
        )
        db.session.add(item)

    db.session.commit()
    flash(f'"{produto.nome}" salvo com sucesso!', 'success')
    return redirect(url_for('produtos.detalhe', id=produto.id))


@produtos_bp.route('/api/nova-mp', methods=['POST'])
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
def excluir(id):
    produto = Produto.query.get_or_404(id)
    nome = produto.nome
    db.session.delete(produto)
    db.session.commit()
    flash(f'"{nome}" excluido!', 'success')
    return redirect(url_for('produtos.lista'))

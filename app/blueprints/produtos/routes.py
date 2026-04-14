from flask import render_template, redirect, url_for, flash, request

from app.blueprints.produtos import produtos_bp
from app.extensions import db
from app.models import Produto, Receita, MateriaPrima


@produtos_bp.route('/')
def lista():
    produtos = Produto.query.order_by(Produto.categoria, Produto.nome).all()

    # Buscar receitas como produtos fabricados (com custo calculado)
    receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
    mp_dict = {mp.nome: mp.custo_por_kg for mp in MateriaPrima.query.all()}

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

        custo_un = custo_total / rendimento if rendimento > 0 else 0

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

    return render_template('produtos/lista.html', produtos=produtos, fabricados=fabricados)


@produtos_bp.route('/salvar', methods=['POST'])
def salvar():
    ids = request.form.getlist('produto_id[]')
    nomes = request.form.getlist('nome[]')
    categorias = request.form.getlist('categoria[]')
    descricoes = request.form.getlist('descricao[]')
    atacados = request.form.getlist('preco_atacado[]')
    lojas = request.form.getlist('preco_loja[]')
    sites = request.form.getlist('preco_site[]')

    for i in range(len(nomes)):
        nome = nomes[i].strip()
        if not nome:
            continue

        pid = ids[i].strip() if i < len(ids) else ''

        if pid:
            produto = Produto.query.get(int(pid))
            if not produto:
                continue
        else:
            produto = Produto()
            db.session.add(produto)

        produto.nome = nome
        produto.categoria = categorias[i].strip() if i < len(categorias) else ''
        produto.descricao = descricoes[i].strip() if i < len(descricoes) else ''

        at = atacados[i].replace(',', '.').strip() if i < len(atacados) else ''
        produto.preco_atacado = float(at) if at else None
        lj = lojas[i].replace(',', '.').strip() if i < len(lojas) else ''
        produto.preco_loja = float(lj) if lj else None
        st = sites[i].replace(',', '.').strip() if i < len(sites) else ''
        produto.preco_site = float(st) if st else None

    db.session.commit()
    flash('Produtos salvos com sucesso!', 'success')
    return redirect(url_for('produtos.lista'))


@produtos_bp.route('/excluir/<int:id>', methods=['POST'])
def excluir(id):
    produto = Produto.query.get_or_404(id)
    nome = produto.nome
    db.session.delete(produto)
    db.session.commit()
    flash(f'"{nome}" excluido!', 'success')
    return redirect(url_for('produtos.lista'))

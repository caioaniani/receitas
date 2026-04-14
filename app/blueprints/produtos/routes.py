from flask import render_template, redirect, url_for, flash, request

from app.blueprints.produtos import produtos_bp
from app.extensions import db
from app.models import Produto


@produtos_bp.route('/')
def lista():
    produtos = Produto.query.order_by(Produto.categoria, Produto.nome).all()
    return render_template('produtos/lista.html', produtos=produtos)


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

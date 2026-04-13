from flask import render_template, redirect, url_for, flash, request

from app.blueprints.receitas import receitas_bp
from app.extensions import db
from app.models import MateriaPrima, Receita, ReceitaIngrediente


@receitas_bp.route('/<int:id>')
def ficha(id):
    receita = Receita.query.get_or_404(id)
    mp_dict = {mp.nome: mp for mp in MateriaPrima.query.all()}
    return render_template('receitas/ficha.html', receita=receita, mp_dict=mp_dict)


@receitas_bp.route('/<int:id>/salvar', methods=['POST'])
def salvar(id):
    receita = Receita.query.get_or_404(id)

    receita.nome = request.form.get('nome', receita.nome).strip()
    receita.categoria = request.form.get('categoria', '').strip() or None
    preco = request.form.get('preco_venda', '').replace(',', '.').strip()
    receita.preco_venda = float(preco) if preco else None
    receita.rendimento_qtd = float(request.form.get('rendimento_qtd', '1').replace(',', '.'))
    receita.rendimento_unidade = request.form.get('rendimento_unidade', 'unidades').strip()
    receita.peso_base = float(request.form.get('peso_base', '1000').replace(',', '.'))

    # Atualiza ingredientes
    ReceitaIngrediente.query.filter_by(receita_id=receita.id).delete()

    nomes = request.form.getlist('ingrediente_nome[]')
    porcentagens = request.form.getlist('porcentagem[]')
    bases = request.form.getlist('eh_base[]')
    notas = request.form.getlist('nota[]')

    for i in range(len(nomes)):
        nome = nomes[i].strip()
        pct_str = porcentagens[i].replace(',', '.').strip()
        if not nome or not pct_str:
            continue
        ing = ReceitaIngrediente(
            receita_id=receita.id,
            ingrediente_nome=nome,
            porcentagem=float(pct_str),
            eh_base=(bases[i] == '1') if i < len(bases) else False,
            nota=notas[i].strip() if i < len(notas) else None,
        )
        db.session.add(ing)

    db.session.commit()
    flash('Ficha salva com sucesso!', 'success')
    return redirect(url_for('receitas.ficha', id=receita.id))


@receitas_bp.route('/nova', methods=['POST'])
def nova():
    receita = Receita(
        nome='Novo Produto',
        categoria='',
        rendimento_qtd=1,
        rendimento_unidade='unidades',
        peso_base=1000,
    )
    db.session.add(receita)
    db.session.commit()
    flash('Novo produto criado!', 'success')
    return redirect(url_for('receitas.ficha', id=receita.id))


@receitas_bp.route('/<int:id>/excluir', methods=['POST'])
def excluir(id):
    receita = Receita.query.get_or_404(id)
    nome = receita.nome
    db.session.delete(receita)
    db.session.commit()
    flash(f'"{nome}" excluído com sucesso!', 'success')
    return redirect(url_for('materias_primas.banco'))

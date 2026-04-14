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
    preco_loja = request.form.get('preco_loja', '').replace(',', '.').strip()
    receita.preco_loja = float(preco_loja) if preco_loja else None
    preco_site = request.form.get('preco_site', '').replace(',', '.').strip()
    receita.preco_site = float(preco_site) if preco_site else None
    receita.rendimento_qtd = float(request.form.get('rendimento_qtd', '1').replace(',', '.'))
    receita.rendimento_unidade = request.form.get('rendimento_unidade', 'unidades').strip()
    receita.peso_base = float(request.form.get('peso_base', '1000').replace(',', '.'))
    peso_un = request.form.get('peso_unitario', '').replace(',', '.').strip()
    receita.peso_unitario = float(peso_un) if peso_un else None
    perda = request.form.get('perda_percentual', '').replace(',', '.').strip()
    receita.perda_percentual = float(perda) if perda else 0

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


@receitas_bp.route('/<int:id>/duplicar', methods=['POST'])
def duplicar(id):
    original = Receita.query.get_or_404(id)
    copia = Receita(
        nome=f'Cópia de {original.nome}',
        categoria=original.categoria,
        preco_venda=original.preco_venda,
        preco_loja=original.preco_loja,
        preco_site=original.preco_site,
        rendimento_qtd=original.rendimento_qtd,
        rendimento_unidade=original.rendimento_unidade,
        peso_base=original.peso_base,
        peso_unitario=original.peso_unitario,
        perda_percentual=original.perda_percentual,
    )
    db.session.add(copia)
    db.session.flush()

    for ing in original.ingredientes:
        novo_ing = ReceitaIngrediente(
            receita_id=copia.id,
            ingrediente_nome=ing.ingrediente_nome,
            porcentagem=ing.porcentagem,
            eh_base=ing.eh_base,
            nota=ing.nota,
        )
        db.session.add(novo_ing)

    db.session.commit()
    flash(f'Receita duplicada: "{copia.nome}"', 'success')
    return redirect(url_for('receitas.ficha', id=copia.id))


@receitas_bp.route('/<int:id>/excluir', methods=['POST'])
def excluir(id):
    receita = Receita.query.get_or_404(id)
    nome = receita.nome
    db.session.delete(receita)
    db.session.commit()
    flash(f'"{nome}" excluído com sucesso!', 'success')
    return redirect(url_for('materias_primas.banco'))

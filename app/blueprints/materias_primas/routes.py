from flask import render_template, redirect, url_for, flash, request

from app.blueprints.materias_primas import materias_primas_bp
from app.extensions import db
from app.models import MateriaPrima, ReceitaIngrediente


@materias_primas_bp.route('/')
def banco():
    materias = MateriaPrima.query.order_by(MateriaPrima.id).all()
    return render_template('materias_primas/banco.html', materias=materias)


@materias_primas_bp.route('/salvar', methods=['POST'])
def salvar():
    ids = request.form.getlist('mp_id[]')
    nomes = request.form.getlist('nome[]')
    unidades = request.form.getlist('unidade[]')
    custos = request.form.getlist('custo_por_kg[]')
    fornecedores = request.form.getlist('fornecedor[]')
    observacoes_list = request.form.getlist('observacoes[]')

    for i in range(len(nomes)):
        nome = nomes[i].strip()
        if not nome:
            continue

        custo = custos[i].replace(',', '.')
        mp_id = int(ids[i]) if ids[i] else None

        if mp_id:
            mp = MateriaPrima.query.get(mp_id)
            if mp:
                mp.nome = nome
                mp.unidade = unidades[i]
                mp.custo_por_kg = float(custo)
                mp.fornecedor = fornecedores[i].strip() or None
                mp.observacoes = observacoes_list[i].strip() or None
        else:
            mp = MateriaPrima(
                nome=nome,
                unidade=unidades[i],
                custo_por_kg=float(custo),
                fornecedor=fornecedores[i].strip() or None,
                observacoes=observacoes_list[i].strip() or None,
            )
            db.session.add(mp)

    db.session.commit()
    flash('Banco de matérias-primas salvo com sucesso!', 'success')
    return redirect(url_for('materias_primas.banco'))


@materias_primas_bp.route('/excluir/<int:id>', methods=['POST'])
def excluir(id):
    mp = MateriaPrima.query.get_or_404(id)
    uso = ReceitaIngrediente.query.filter_by(ingrediente_nome=mp.nome).first()
    if uso:
        flash(f'Não é possível excluir "{mp.nome}": usado em receitas.', 'danger')
    else:
        db.session.delete(mp)
        db.session.commit()
        flash(f'"{mp.nome}" excluído com sucesso!', 'success')
    return redirect(url_for('materias_primas.banco'))

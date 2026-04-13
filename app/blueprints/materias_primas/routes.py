from flask import render_template, redirect, url_for, flash, request

from app.blueprints.materias_primas import materias_primas_bp
from app.blueprints.materias_primas.forms import MateriaPrimaForm
from app.extensions import db
from app.models import MateriaPrima, ReceitaIngrediente


@materias_primas_bp.route('/')
def listar():
    q = request.args.get('q', '').strip()
    if q:
        filtro = f'%{q}%'
        materias = MateriaPrima.query.filter(
            db.or_(
                MateriaPrima.nome.ilike(filtro),
                MateriaPrima.fornecedor.ilike(filtro),
            )
        ).order_by(MateriaPrima.nome).all()
    else:
        materias = MateriaPrima.query.order_by(MateriaPrima.nome).all()
    return render_template('materias_primas/lista.html', materias=materias, q=q)


@materias_primas_bp.route('/nova', methods=['GET', 'POST'])
def criar():
    form = MateriaPrimaForm()
    if form.validate_on_submit():
        mp = MateriaPrima(
            nome=form.nome.data,
            unidade=form.unidade.data,
            preco=float(form.preco.data),
            fornecedor=form.fornecedor.data or None,
        )
        db.session.add(mp)
        db.session.commit()
        flash('Matéria-prima cadastrada com sucesso!', 'success')
        return redirect(url_for('materias_primas.listar'))
    return render_template('materias_primas/form.html', form=form, titulo='Nova Matéria-Prima')


@materias_primas_bp.route('/<int:id>')
def detalhe(id):
    mp = MateriaPrima.query.get_or_404(id)
    receitas_uso = (
        db.session.query(ReceitaIngrediente)
        .filter_by(materia_prima_id=mp.id)
        .all()
    )
    return render_template('materias_primas/detalhe.html', mp=mp, receitas_uso=receitas_uso)


@materias_primas_bp.route('/<int:id>/editar', methods=['GET', 'POST'])
def editar(id):
    mp = MateriaPrima.query.get_or_404(id)
    form = MateriaPrimaForm(obj=mp)
    if form.validate_on_submit():
        mp.nome = form.nome.data
        mp.unidade = form.unidade.data
        mp.preco = float(form.preco.data)
        mp.fornecedor = form.fornecedor.data or None
        db.session.commit()
        flash('Matéria-prima atualizada com sucesso!', 'success')
        return redirect(url_for('materias_primas.detalhe', id=mp.id))
    return render_template('materias_primas/form.html', form=form, titulo='Editar Matéria-Prima', mp=mp)


@materias_primas_bp.route('/<int:id>/excluir', methods=['POST'])
def excluir(id):
    mp = MateriaPrima.query.get_or_404(id)
    uso = ReceitaIngrediente.query.filter_by(materia_prima_id=mp.id).first()
    if uso:
        flash('Não é possível excluir: esta matéria-prima é usada em receitas.', 'danger')
        return redirect(url_for('materias_primas.detalhe', id=mp.id))
    db.session.delete(mp)
    db.session.commit()
    flash('Matéria-prima excluída com sucesso!', 'success')
    return redirect(url_for('materias_primas.listar'))

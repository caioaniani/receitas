from flask import render_template, redirect, url_for, flash, request

from app.blueprints.receitas import receitas_bp
from app.blueprints.receitas.forms import ReceitaForm, PrecificacaoForm
from app.extensions import db
from app.models import MateriaPrima, Receita, ReceitaIngrediente


@receitas_bp.route('/')
def listar():
    receitas = Receita.query.order_by(Receita.nome).all()
    return render_template('receitas/lista.html', receitas=receitas)


@receitas_bp.route('/nova', methods=['GET', 'POST'])
def criar():
    form = ReceitaForm()
    materias = MateriaPrima.query.order_by(MateriaPrima.nome).all()

    if form.validate_on_submit():
        receita = Receita(
            nome=form.nome.data,
            categoria=form.categoria.data,
            rendimento_qtd=float(form.rendimento_qtd.data),
            rendimento_unidade=form.rendimento_unidade.data,
            margem_lucro=float(form.margem_lucro.data) if form.margem_lucro.data else None,
            custo_adicional_pct=float(form.custo_adicional_pct.data) if form.custo_adicional_pct.data else None,
            custo_adicional_fixo=float(form.custo_adicional_fixo.data) if form.custo_adicional_fixo.data else None,
        )
        db.session.add(receita)
        db.session.flush()

        ids = request.form.getlist('ingrediente_id[]')
        qtds = request.form.getlist('quantidade[]')
        bases = request.form.getlist('eh_base[]')

        for i, (mp_id, qtd) in enumerate(zip(ids, qtds)):
            if mp_id and qtd:
                ing = ReceitaIngrediente(
                    receita_id=receita.id,
                    materia_prima_id=int(mp_id),
                    quantidade=float(qtd),
                    eh_base=str(i) in bases,
                )
                db.session.add(ing)

        db.session.commit()
        flash('Receita cadastrada com sucesso!', 'success')
        return redirect(url_for('receitas.detalhe', id=receita.id))

    return render_template('receitas/form.html', form=form, materias=materias, titulo='Nova Receita')


@receitas_bp.route('/<int:id>')
def detalhe(id):
    receita = Receita.query.get_or_404(id)
    tem_base = any(ing.eh_base for ing in receita.ingredientes)
    return render_template('receitas/detalhe.html', receita=receita, tem_base=tem_base)


@receitas_bp.route('/<int:id>/editar', methods=['GET', 'POST'])
def editar(id):
    receita = Receita.query.get_or_404(id)
    form = ReceitaForm(obj=receita)
    materias = MateriaPrima.query.order_by(MateriaPrima.nome).all()

    if form.validate_on_submit():
        receita.nome = form.nome.data
        receita.categoria = form.categoria.data
        receita.rendimento_qtd = float(form.rendimento_qtd.data)
        receita.rendimento_unidade = form.rendimento_unidade.data
        receita.margem_lucro = float(form.margem_lucro.data) if form.margem_lucro.data else None
        receita.custo_adicional_pct = float(form.custo_adicional_pct.data) if form.custo_adicional_pct.data else None
        receita.custo_adicional_fixo = float(form.custo_adicional_fixo.data) if form.custo_adicional_fixo.data else None

        # Remove ingredientes antigos
        ReceitaIngrediente.query.filter_by(receita_id=receita.id).delete()

        ids = request.form.getlist('ingrediente_id[]')
        qtds = request.form.getlist('quantidade[]')
        bases = request.form.getlist('eh_base[]')

        for i, (mp_id, qtd) in enumerate(zip(ids, qtds)):
            if mp_id and qtd:
                ing = ReceitaIngrediente(
                    receita_id=receita.id,
                    materia_prima_id=int(mp_id),
                    quantidade=float(qtd),
                    eh_base=str(i) in bases,
                )
                db.session.add(ing)

        db.session.commit()
        flash('Receita atualizada com sucesso!', 'success')
        return redirect(url_for('receitas.detalhe', id=receita.id))

    return render_template('receitas/form.html', form=form, materias=materias, titulo='Editar Receita', receita=receita)


@receitas_bp.route('/<int:id>/excluir', methods=['POST'])
def excluir(id):
    receita = Receita.query.get_or_404(id)
    db.session.delete(receita)
    db.session.commit()
    flash('Receita excluída com sucesso!', 'success')
    return redirect(url_for('receitas.listar'))


@receitas_bp.route('/<int:id>/precificacao', methods=['GET', 'POST'])
def precificacao(id):
    receita = Receita.query.get_or_404(id)
    form = PrecificacaoForm(obj=receita)

    if form.validate_on_submit():
        receita.margem_lucro = float(form.margem_lucro.data)
        receita.custo_adicional_pct = float(form.custo_adicional_pct.data) if form.custo_adicional_pct.data else None
        receita.custo_adicional_fixo = float(form.custo_adicional_fixo.data) if form.custo_adicional_fixo.data else None
        db.session.commit()
        flash('Precificação atualizada com sucesso!', 'success')
        return redirect(url_for('receitas.detalhe', id=receita.id))

    return render_template('receitas/precificacao.html', form=form, receita=receita)

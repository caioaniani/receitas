from flask import render_template

from app.blueprints.relatorios import relatorios_bp
from app.extensions import db
from app.models import MateriaPrima, Receita, ReceitaIngrediente


@relatorios_bp.route('/custos')
def custos():
    receitas = Receita.query.order_by(Receita.nome).all()
    # Ordena por custo total (decrescente)
    receitas_sorted = sorted(receitas, key=lambda r: r.custo_total, reverse=True)
    return render_template('relatorios/custos.html', receitas=receitas_sorted)


@relatorios_bp.route('/ingredientes')
def ingredientes():
    materias = (
        db.session.query(
            MateriaPrima,
            db.func.count(ReceitaIngrediente.id).label('uso_count')
        )
        .outerjoin(ReceitaIngrediente, MateriaPrima.id == ReceitaIngrediente.materia_prima_id)
        .group_by(MateriaPrima.id)
        .order_by(MateriaPrima.preco.desc())
        .all()
    )
    return render_template('relatorios/ingredientes.html', materias=materias)

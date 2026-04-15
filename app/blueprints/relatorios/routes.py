from flask import render_template
from flask_login import login_required

from app.blueprints.relatorios import relatorios_bp
from app.decorators import admin_required
from app.extensions import db
from app.models import MateriaPrima, Receita, ReceitaIngrediente


@relatorios_bp.route('/custos')
@login_required
@admin_required
def custos():
    # Placeholder — será reescrito na Fase 2.2 com o serviço de custos
    receitas = Receita.query.order_by(Receita.nome).all()
    return render_template('relatorios/custos.html', receitas=receitas)


@relatorios_bp.route('/ingredientes')
@login_required
@admin_required
def ingredientes():
    # Join por nome (ingrediente_nome é string, não FK)
    materias = (
        db.session.query(
            MateriaPrima,
            db.func.count(ReceitaIngrediente.id).label('uso_count')
        )
        .outerjoin(ReceitaIngrediente, MateriaPrima.nome == ReceitaIngrediente.ingrediente_nome)
        .group_by(MateriaPrima.id)
        .order_by(MateriaPrima.custo_por_kg.desc())
        .all()
    )
    return render_template('relatorios/ingredientes.html', materias=materias)

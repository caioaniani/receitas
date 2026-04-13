from flask import render_template

from app.blueprints.main import main_bp
from app.extensions import db
from app.models import MateriaPrima, Receita, ReceitaIngrediente


@main_bp.route('/')
def index():
    total_materias = MateriaPrima.query.count()
    total_receitas = Receita.query.count()

    receitas_recentes = Receita.query.order_by(Receita.criado_em.desc()).limit(5).all()

    # Ingredientes mais usados
    ingredientes_populares = (
        db.session.query(
            MateriaPrima.nome,
            db.func.count(ReceitaIngrediente.id).label('uso_count')
        )
        .join(ReceitaIngrediente, MateriaPrima.id == ReceitaIngrediente.materia_prima_id)
        .group_by(MateriaPrima.id)
        .order_by(db.text('uso_count DESC'))
        .limit(5)
        .all()
    )

    return render_template(
        'main/index.html',
        total_materias=total_materias,
        total_receitas=total_receitas,
        receitas_recentes=receitas_recentes,
        ingredientes_populares=ingredientes_populares,
    )

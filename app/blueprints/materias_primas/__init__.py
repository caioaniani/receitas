from flask import Blueprint

materias_primas_bp = Blueprint('materias_primas', __name__, template_folder='../../templates/materias_primas')

from app.blueprints.materias_primas import routes  # noqa: E402, F401

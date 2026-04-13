from flask import Blueprint

receitas_bp = Blueprint('receitas', __name__, template_folder='../../templates/receitas')

from app.blueprints.receitas import routes  # noqa: E402, F401

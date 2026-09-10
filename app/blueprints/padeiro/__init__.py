from flask import Blueprint

padeiro_bp = Blueprint('padeiro', __name__,
                       template_folder='../../templates/padeiro')

from app.blueprints.padeiro import diario_routes, routes  # noqa: E402,F401

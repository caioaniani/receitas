from flask import Blueprint

padeiro_testes_bp = Blueprint('padeiro_testes', __name__,
                       template_folder='../../templates/padeiro_testes')

from app.blueprints.padeiro_testes import routes  # noqa: E402,F401

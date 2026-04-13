from flask import Blueprint

relatorios_bp = Blueprint('relatorios', __name__, template_folder='../../templates/relatorios')

from app.blueprints.relatorios import routes  # noqa: E402, F401

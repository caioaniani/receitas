from flask import Blueprint

precadastro_bp = Blueprint('precadastro', __name__)

from app.blueprints.precadastro import routes  # noqa: E402,F401

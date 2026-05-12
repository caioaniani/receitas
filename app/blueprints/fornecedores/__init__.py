from flask import Blueprint

fornecedores_bp = Blueprint('fornecedores', __name__, url_prefix='/fornecedores')

from app.blueprints.fornecedores import routes  # noqa: E402,F401

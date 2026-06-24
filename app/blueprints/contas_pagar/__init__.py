from flask import Blueprint

contas_pagar_bp = Blueprint('contas_pagar', __name__, url_prefix='/contas-pagar')

from app.blueprints.contas_pagar import routes  # noqa: E402,F401

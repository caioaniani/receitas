from flask import Blueprint

cobrancas_bp = Blueprint('cobrancas', __name__)

from app.blueprints.cobrancas import routes  # noqa: E402, F401

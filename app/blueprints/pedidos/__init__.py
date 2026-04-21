from flask import Blueprint

pedidos_bp = Blueprint('pedidos', __name__, url_prefix='/pedidos')

from app.blueprints.pedidos import routes  # noqa

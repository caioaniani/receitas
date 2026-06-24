from flask import Blueprint

lista_compras_bp = Blueprint('lista_compras', __name__,
                             url_prefix='/lista-compras')

from app.blueprints.lista_compras import routes  # noqa: E402, F401

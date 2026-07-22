from flask import Blueprint

treinamento_bp = Blueprint('treinamento', __name__, url_prefix='/treinamento')

from app.blueprints.treinamento import routes  # noqa: E402,F401

from flask import Blueprint

producao_bp = Blueprint('producao', __name__)

from app.blueprints.producao import routes  # noqa

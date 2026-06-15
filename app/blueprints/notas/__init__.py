from flask import Blueprint

notas_bp = Blueprint('notas', __name__, url_prefix='/notas')

from app.blueprints.notas import routes  # noqa: E402,F401

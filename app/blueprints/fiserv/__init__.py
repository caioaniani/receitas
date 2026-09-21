from flask import Blueprint

fiserv_bp = Blueprint('fiserv', __name__, url_prefix='/financeiro/fiserv')

from app.blueprints.fiserv import routes  # noqa: E402, F401

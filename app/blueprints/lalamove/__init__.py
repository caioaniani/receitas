from flask import Blueprint

lalamove_bp = Blueprint('lalamove', __name__, url_prefix='/lalamove')

from app.blueprints.lalamove import routes  # noqa: E402,F401

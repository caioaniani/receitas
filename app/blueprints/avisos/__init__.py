from flask import Blueprint

avisos_bp = Blueprint('avisos', __name__, url_prefix='/avisos')

from app.blueprints.avisos import routes  # noqa: E402,F401

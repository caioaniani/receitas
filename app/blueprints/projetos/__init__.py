from flask import Blueprint

projetos_bp = Blueprint('projetos', __name__, url_prefix='/projetos')

from app.blueprints.projetos import routes  # noqa: E402,F401

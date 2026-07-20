from flask import Blueprint

patrimonio_bp = Blueprint('patrimonio', __name__)

from app.blueprints.patrimonio import routes  # noqa: E402, F401

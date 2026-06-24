from flask import Blueprint

b2b_bp = Blueprint('b2b', __name__, template_folder='../../templates/b2b')

from app.blueprints.b2b import routes  # noqa: E402, F401

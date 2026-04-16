from flask import Blueprint

rh_bp = Blueprint('rh', __name__, template_folder='../../templates/rh')

from app.blueprints.rh import routes  # noqa: E402, F401

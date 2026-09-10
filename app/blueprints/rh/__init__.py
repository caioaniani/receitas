from flask import Blueprint

rh_bp = Blueprint('rh', __name__, template_folder='../../templates/rh')

from app.blueprints.rh import equipe_routes, promocao_routes, routes  # noqa: E402, F401

from flask import Blueprint

rh_bp = Blueprint('rh', __name__, template_folder='../../templates/rh')

from app.blueprints.rh import (  # noqa: E402, F401
    equipe_routes,
    plano_carreira_lote_routes,
    promocao_routes,
    routes,
)

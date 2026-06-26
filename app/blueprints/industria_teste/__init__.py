from flask import Blueprint

industria_teste_bp = Blueprint(
    'industria_teste', __name__,
    template_folder='../../templates/industria_teste')

from app.blueprints.industria_teste import routes  # noqa: E402,F401

from flask import Blueprint

entregas_bp = Blueprint('entregas', __name__, template_folder='../../templates/entregas')

from app.blueprints.entregas import routes  # noqa

from flask import Blueprint

comprovante_bp = Blueprint('comprovante', __name__, template_folder='../../templates/driver')

from app.blueprints.comprovante import routes  # noqa

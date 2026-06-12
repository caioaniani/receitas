from flask import Blueprint

pdv_bp = Blueprint('pdv', __name__, template_folder='../../templates/pdv')

from app.blueprints.pdv import routes  # noqa
from app.blueprints.pdv import caixa  # noqa
from app.blueprints.pdv import sync_api  # noqa

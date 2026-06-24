from flask import Blueprint

crm_bp = Blueprint('crm', __name__, url_prefix='/crm')

from app.blueprints.crm import routes  # noqa: E402,F401

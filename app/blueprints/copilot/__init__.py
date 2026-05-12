from flask import Blueprint

copilot_bp = Blueprint('copilot', __name__, url_prefix='/copilot')

from app.blueprints.copilot import routes  # noqa: E402,F401

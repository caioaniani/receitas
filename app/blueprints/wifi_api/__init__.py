from flask import Blueprint

wifi_api_bp = Blueprint('wifi_api', __name__)

from app.blueprints.wifi_api import routes  # noqa: E402,F401

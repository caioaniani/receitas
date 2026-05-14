from flask import Blueprint

slack_bp = Blueprint('slack', __name__, url_prefix='/slack')

from app.blueprints.slack import routes  # noqa: E402,F401

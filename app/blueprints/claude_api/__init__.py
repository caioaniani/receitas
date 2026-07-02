from flask import Blueprint

claude_api_bp = Blueprint('claude_api', __name__)

from app.blueprints.claude_api import routes  # noqa: E402,F401

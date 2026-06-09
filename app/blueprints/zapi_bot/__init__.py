from flask import Blueprint

zapi_bot_bp = Blueprint('zapi_bot', __name__, url_prefix='/zapi')

from app.blueprints.zapi_bot import routes  # noqa: E402,F401

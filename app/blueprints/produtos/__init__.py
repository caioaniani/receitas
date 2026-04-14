from flask import Blueprint

produtos_bp = Blueprint('produtos', __name__)

from app.blueprints.produtos import routes  # noqa

from flask import Blueprint

checklist_bp = Blueprint('checklist', __name__)

from app.blueprints.checklist import routes  # noqa: E402, F401

from flask import Blueprint

kits_cafe_admin_bp = Blueprint('kits_cafe_admin', __name__, url_prefix='/admin/kits-cafe')

from app.blueprints.kits_cafe_admin import routes  # noqa: E402,F401

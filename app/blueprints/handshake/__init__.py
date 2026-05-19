from flask import Blueprint

handshake_bp = Blueprint('handshake', __name__,
                          template_folder='../../templates/handshake')

from app.blueprints.handshake import routes  # noqa: E402, F401

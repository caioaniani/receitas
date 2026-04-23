from functools import wraps

from flask import abort
from flask_login import current_user


def admin_required(f):
    """Bloqueia acesso para usuários que não são admin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin():
            abort(403)
        return f(*args, **kwargs)
    return decorated


def entrega_access_required(f):
    """Permite acesso para admin ou funcionario vinculado a uma loja."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin() and not current_user.loja_id:
            abort(403)
        return f(*args, **kwargs)
    return decorated

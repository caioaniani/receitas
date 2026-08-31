from functools import wraps

from flask import abort
from flask_login import current_user


def _pode_cap(capacidade):
    """admin/owner sempre liberados; demais papeis consultam o modelo editavel
    (app/services/permissoes.py). Padroes espelham o comportamento legado, entao
    sem overrides no banco o resultado e identico ao de antes."""
    if current_user.is_admin():  # is_admin() ja inclui o owner
        return True
    from app.services import permissoes
    return permissoes.pode(getattr(current_user, 'papel', '') or '', capacidade)


def admin_required(f):
    """Bloqueia acesso para usuários que não são admin (ou owner). Fixo (não editável)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin():
            abort(403)
        return f(*args, **kwargs)
    return decorated


def gerente_required(f):
    """Estoque de loja / relatório / preços. Capacidade editável: web_estoque_loja."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _pode_cap('web_estoque_loja'):
            abort(403)
        return f(*args, **kwargs)
    return decorated


def producao_required(f):
    """Plano / Congelados / Separação. Capacidade editável: web_producao."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _pode_cap('web_producao'):
            abort(403)
        return f(*args, **kwargs)
    return decorated


def operacional_pedido_required(f):
    """Mudar status de pedido (confirmar/separar/enviar/cancelar/receber).
    Capacidade editável: web_pedido_operar."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _pode_cap('web_pedido_operar'):
            abort(403)
        return f(*args, **kwargs)
    return decorated


def padeiro_required(f):
    """Tela touchscreen do padeiro (chao de fabrica). Capacidade editável: web_padeiro."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _pode_cap('web_padeiro'):
            abort(403)
        return f(*args, **kwargs)
    return decorated


def catalogo_required(f):
    """Receitas / MP / Produtos / Fornecedores (leitura + estoque MP).
    Capacidade editável: web_catalogo."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _pode_cap('web_catalogo'):
            abort(403)
        return f(*args, **kwargs)
    return decorated


def rh_required(f):
    """Ponto / Férias / Cargos. Capacidade editável: web_rh."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _pode_cap('web_rh'):
            abort(403)
        return f(*args, **kwargs)
    return decorated


def checklist_required(f):
    """Checklist de loja (abertura/troca de turno/fechamento) — quem o dono
    pediu foi o responsável do turno. Capacidade editável: web_checklist
    (default gerente+funcionario; admin/owner sempre)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _pode_cap('web_checklist'):
            abort(403)
        return f(*args, **kwargs)
    return decorated


def owner_required(f):
    """Bloqueia acesso para usuários que não são super admin (is_owner=True).
    Use em telas/endpoints que envolvem salários e dados financeiros sensíveis.
    Fixo (não editável) — tier owner."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not getattr(current_user, 'is_owner', False):
            abort(403)
        return f(*args, **kwargs)
    return decorated


def divulgacao_required(f):
    """Lancar/gerenciar DIVULGACAO (brinde/PR): SO o dono e o papel 'marketing'
    (decisao do dono 21/07/2026 — 'so o owner e marketing'). Admin comum NAO
    entra. Fixo (nao editavel) — e o gesto de dar produto de graca."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not (getattr(current_user, 'pode_divulgacao', None)
                and current_user.pode_divulgacao()):
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


def pedidos_required(f):
    """Acessar telas de pedido (ver / criar). Capacidade editável: web_pedidos
    (padrão: todos os papéis menos padeiro)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _pode_cap('web_pedidos'):
            abort(403)
        return f(*args, **kwargs)
    return decorated


def consulta_pedidos_required(f):
    """Áreas operacionais somente leitura: owner/admin ou observador."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not (current_user.is_admin()
                or getattr(current_user, 'is_observador', lambda: False)()):
            abort(403)
        return f(*args, **kwargs)
    return decorated

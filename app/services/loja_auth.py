"""Auth do cliente final da loja online (Fase 6).

SESSÃO SEPARADA do Flask-Login do admin: o staff usa `current_user`
(`Usuario` com papel interno); o cliente final usa `cliente_atual()`
(`Cliente` do modelo `loja_online`). São duas autenticações distintas no
MESMO cookie de sessão Flask — chaves diferentes (`_user_id` vs.
`cliente_id`) — pra que privilégio NÃO cruze: cliente nunca acessa admin,
e vice-versa. Quem está logado no admin pode até "ver" a loja como
visitante, mas não vira `cliente_atual` automaticamente.

Senha em scrypt (`Cliente.set_senha`), mesmo padrão de `Usuario`.
"""
from functools import wraps

from flask import abort, g, redirect, request, session, url_for


def cliente_atual():
    """Devolve o `Cliente` logado (ou None). Cacheado em `g` por request."""
    if 'cliente' in g:
        return g.cliente
    from app.models import Cliente
    cid = session.get('cliente_id')
    c = Cliente.query.get(cid) if cid else None
    if c and not c.ativo:
        c = None       # conta desativada — sai
    g.cliente = c
    return c


def login_cliente(cliente):
    """Cria sessão de cliente (login OK). Apaga sessão antiga, se houver."""
    session['cliente_id'] = cliente.id
    g.cliente = cliente


def logout_cliente():
    session.pop('cliente_id', None)
    g.pop('cliente', None)


def cliente_required(f):
    """Exige cliente logado; redireciona pra /loja/entrar com `next`."""
    @wraps(f)
    def wrapper(*a, **kw):
        if not cliente_atual():
            return redirect(url_for('loja.entrar', next=request.path))
        return f(*a, **kw)
    return wrapper


def safe_next():
    """Lê `next` da query/form e devolve só se for path interno da loja
    (anti open-redirect). Sem isso, atacante manda
    `/loja/entrar?next=//evil.com` e o redirect leva pra fora."""
    nxt = (request.values.get('next') or '').strip()
    if (nxt.startswith('/loja/') and not nxt.startswith('/loja//')
            and '\\' not in nxt and '://' not in nxt):
        return nxt
    return None


def email_valido(email):
    return ('@' in email and '.' in email.split('@')[-1]
            and 5 <= len(email) <= 200)


def _abort_se_nao_cliente():
    """Para uso em hooks; aborta 404 (não 403, pra não confessar a rota)."""
    if not cliente_atual():
        abort(404)

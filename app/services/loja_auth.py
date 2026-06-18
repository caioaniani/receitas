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

from flask import g, redirect, request, session, url_for


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


# ── Reset de senha (Fase 6 — PR 3) ─────────────────────────────────────
# Anti-enumeração: SEMPRE devolver a mesma mensagem ao usuário ("se houver
# uma conta, mandamos um link"), independente de o email existir. Sem isso,
# atacante varre a base por enumeração nos retornos diferentes.

import secrets  # noqa: E402
from datetime import timedelta  # noqa: E402


def _hashear_email(email):
    return (email or '').strip().lower()


def iniciar_reset(email):
    """Cria um token de reset (se houver conta com esse email) e dispara o
    e-mail. SEMPRE devolve True (não confessa se a conta existe — anti
    enumeração). Best-effort: e-mail falhando não levanta exceção.

    Token expira em 1h. Múltiplas chamadas seguidas criam tokens novos —
    os antigos continuam válidos até expirar (não invalido por simplicidade
    de UX: se o cliente clicou no link anterior, ele ainda funciona)."""
    from app.extensions import db
    from app.models import Cliente, ClienteResetSenha
    from app.services import email as email_svc
    from app.utils import agora
    email_norm = _hashear_email(email)
    if not email_valido(email_norm):
        return True   # mesmo erro genérico
    cli = Cliente.query.filter(
        db.func.lower(Cliente.email) == email_norm).first()
    if cli and cli.ativo and cli.senha_hash:
        # Só cria token se a conta de fato tem senha (cliente cadastrado).
        # Guest sem senha não recebe link — ele se cadastra do zero.
        token = secrets.token_urlsafe(32)
        reg = ClienteResetSenha(
            cliente_id=cli.id, token=token,
            expira_em=agora() + timedelta(hours=1))
        db.session.add(reg)
        db.session.commit()
        try:
            email_svc.enviar_reset_senha(cli, token)
        except Exception:  # noqa: BLE001
            pass   # best-effort
    return True


def token_reset_valido(token):
    """Devolve `ClienteResetSenha` válido (não usado, não expirado) ou None."""
    from app.models import ClienteResetSenha
    from app.utils import agora
    if not token or len(token) < 20:
        return None
    reg = ClienteResetSenha.query.filter_by(token=token).first()
    if not reg or not reg.valido(agora()):
        return None
    return reg


def aplicar_reset(token, nova_senha):
    """Aplica o reset. Devolve {ok, erro?}. Marca token como usado mesmo
    se a senha não bater regras — não cria janela de retry com o mesmo
    token (cliente pede outro)."""
    from app.extensions import db
    from app.utils import agora
    reg = token_reset_valido(token)
    if not reg:
        return {'ok': False, 'erro': 'Link expirado ou inválido. Peça um novo.'}
    if not nova_senha or len(nova_senha) < 8:
        return {'ok': False, 'erro': 'A senha precisa ter ao menos 8 caracteres.'}
    reg.cliente.set_senha(nova_senha)
    reg.usado_em = agora()
    db.session.commit()
    return {'ok': True, 'cliente': reg.cliente}

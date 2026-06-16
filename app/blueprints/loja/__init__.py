"""Loja Online — vitrine pública (Fase 2, 16/06/2026).

Plano completo em /root/.claude/plans/modular-tinkering-owl.md.

Gate de acesso (`LOJA_VISIVEL=0` por padrão durante desenvolvimento):
visitantes anônimos recebem 404 nas rotas públicas; staff logado no admin
vê normalmente. Quando `LOJA_VISIVEL=1` (Fase 8 — cutover), a loja vira
pública pra qualquer um.
"""
from flask import Blueprint

loja_bp = Blueprint(
    'loja', __name__,
    template_folder='../../templates/loja',
    static_folder='../../static/loja',
    static_url_path='/static/loja',
)

from app.blueprints.loja import routes  # noqa: E402,F401

from flask import Blueprint

# Sistema de treinamento GAMIFICADO (spec v1.0). Único módulo de treinamento —
# o antigo "Vídeos simples" (/treinamento) foi removido em 24/07/2026.
treino_bp = Blueprint('treino', __name__, url_prefix='/treino')

from app.blueprints.treino import routes  # noqa: E402,F401

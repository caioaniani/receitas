from flask import Blueprint

# Sistema de treinamento GAMIFICADO (spec v1.0). Prefixo /treino pra não colidir
# com o módulo simples de vídeo que já vive em /treinamento.
treino_bp = Blueprint('treino', __name__, url_prefix='/treino')

from app.blueprints.treino import routes  # noqa: E402,F401

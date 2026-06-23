from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
csrf = CSRFProtect()
# Limiter com storage in-memory: limite e por worker. Em multi-worker o
# limite efetivo eh N×worker (ex: 5/min × 2 workers = 10/min reais). Pra
# o volume da padaria, tolerado — atacante de forca bruta nao eh vetor
# realista aqui. Se mudar carga ou exposicao, trocar pra storage_uri='redis://'.
#
# default_limits aplicam a TODAS as rotas que nao tenham `@limiter.limit`
# proprio. Teto global por IP — protege contra varredura de bots e DoS
# leve (ex: atacante pedindo 100k vezes /loja). Endpoints sensiveis
# mantem limites mais apertados via decorator. Webhook do Pagar.me usa
# `@limiter.exempt` (e legitimo o Pagar.me bater muito quando ha picos).
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=['300 per minute', '5000 per hour'],
    storage_uri="memory://",
)
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Faça login para acessar o sistema.'
login_manager.login_message_category = 'warning'
migrate = Migrate()

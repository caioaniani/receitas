"""Re-exporta tudo dos sub-modulos. Compat com `from app.models import X`."""

from app.models.auth import *  # noqa: F401, F403
from app.models.avisos import *  # noqa: F401, F403
from app.models.b2b import *  # noqa: F401, F403
from app.models.catalogo import *  # noqa: F401, F403
from app.models.checklist import *  # noqa: F401, F403
from app.models.cobrancas_automacao import *  # noqa: F401, F403
from app.models.config import *  # noqa: F401, F403
from app.models.entregas import *  # noqa: F401, F403
from app.models.estoque import *  # noqa: F401, F403
from app.models.financeiro import *  # noqa: F401, F403
from app.models.integracoes import *  # noqa: F401, F403
from app.models.lista_compras import *  # noqa: F401, F403
from app.models.loja import *  # noqa: F401, F403
from app.models.loja_online import *  # noqa: F401, F403
from app.models.notas import *  # noqa: F401, F403
from app.models.notificacoes import *  # noqa: F401, F403
from app.models.patrimonio import *  # noqa: F401, F403
from app.models.pedidos import *  # noqa: F401, F403
from app.models.producao import *  # noqa: F401, F403
from app.models.projetos import *  # noqa: F401, F403
from app.models.rh import *  # noqa: F401, F403
from app.models.rh_carreira import *  # noqa: F401, F403
from app.models.treino_conteudo import *  # noqa: F401, F403
from app.models.treino_gamificacao import *  # noqa: F401, F403

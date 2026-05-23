"""Saude do sync de PDV (Seru/VNDA) e reconciliacao venda-vs-estoque.

Da visibilidade sobre vendas que podem NAO ter baixado estoque:
- lojas Seru aguardando confirmacao (fuzzy nao confirmado nao baixa —
  ver seru_sync.py:405);
- produtos pendentes de mapeamento (nao baixam — seru_sync.py:420);
- pedidos registrados sem loja.

A baixa em si esta correta e auditavel (todo movimento gera MovEstoqueLoja);
o risco operacional eh ficar uma venda sem baixar por falta de mapeamento ou
por sync fora do ar. Este modulo expoe isso.
"""
import logging

from app.models import (
    SeruLojaMap,
    SeruPedidoProcessado,
    SeruProdutoMap,
    VndaProdutoMap,
)
from app.utils import agora as _agora

logger = logging.getLogger(__name__)

# Acima disso (minutos desde o ultimo sync) consideramos atrasado. O cron
# roda a cada 15min; 40min cobre 2 ciclos perdidos + folga.
_ATRASO_MIN = 40


def _minutos_desde(dt):
    if not dt:
        return None
    delta = _agora() - dt
    return delta.total_seconds() / 60.0


def resumo():
    """Snapshot da saude do sync. Retorna dict pronto pra template/card.

    Numeros sao counts persistidos (baratos) — nao chama API externa.
    """
    from app.services import seru_cron

    st_seru = seru_cron.status()
    st_vnda = seru_cron.status_vnda()

    min_seru = _minutos_desde(st_seru.get('ultimo_run'))
    min_vnda = _minutos_desde(st_vnda.get('ultimo_run'))

    # Loja pendente = nao confirmada e nao ignorada. Inclui fuzzy auto-match
    # NAO confirmado — esses NAO baixam estoque ate o admin confirmar.
    lojas_pendentes = (SeruLojaMap.query
                       .filter(SeruLojaMap.confirmado_em.is_(None))
                       .filter(SeruLojaMap.ignorar.is_(False))
                       .count())

    # Produto pendente = estado 'pendente' (sem receita/produto, nao ignorado).
    # Esses sao vendidos mas NAO baixam ate mapear.
    produtos_pendentes_seru = (SeruProdutoMap.query
                               .filter(SeruProdutoMap.receita_id.is_(None))
                               .filter(SeruProdutoMap.produto_id.is_(None))
                               .filter(SeruProdutoMap.ignorar.is_(False))
                               .count())
    produtos_pendentes_vnda = (VndaProdutoMap.query
                               .filter(VndaProdutoMap.receita_id.is_(None))
                               .filter(VndaProdutoMap.produto_id.is_(None))
                               .filter(VndaProdutoMap.ignorar.is_(False))
                               .count())

    # Pedidos registrados sem loja resolvida (informativo — registrados mas
    # nao baixaram porque a loja Seru nao casou com nenhuma Loja).
    pedidos_sem_loja = (SeruPedidoProcessado.query
                        .filter(SeruPedidoProcessado.loja_id.is_(None))
                        .count())

    total_pendencias = (lojas_pendentes + produtos_pendentes_seru
                        + produtos_pendentes_vnda + pedidos_sem_loja)

    return {
        'seru_ultimo_run': st_seru.get('ultimo_run'),
        'seru_ativo': st_seru.get('ativo'),
        'seru_atrasado': (min_seru is None) or (min_seru > _ATRASO_MIN),
        'vnda_ultimo_run': st_vnda.get('ultimo_run'),
        'vnda_ativo': st_vnda.get('ativo'),
        'vnda_atrasado': (min_vnda is None) or (min_vnda > _ATRASO_MIN),
        'lojas_pendentes': lojas_pendentes,
        'produtos_pendentes_seru': produtos_pendentes_seru,
        'produtos_pendentes_vnda': produtos_pendentes_vnda,
        'pedidos_sem_loja': pedidos_sem_loja,
        'total_pendencias': total_pendencias,
    }


def contar_pendencias():
    """So o total de pendencias acionaveis — pro card do dashboard.

    Barato: 4 counts. Nao inclui status de atraso (esse so no painel).
    """
    lojas = (SeruLojaMap.query
             .filter(SeruLojaMap.confirmado_em.is_(None))
             .filter(SeruLojaMap.ignorar.is_(False)).count())
    prod_seru = (SeruProdutoMap.query
                 .filter(SeruProdutoMap.receita_id.is_(None))
                 .filter(SeruProdutoMap.produto_id.is_(None))
                 .filter(SeruProdutoMap.ignorar.is_(False)).count())
    prod_vnda = (VndaProdutoMap.query
                 .filter(VndaProdutoMap.receita_id.is_(None))
                 .filter(VndaProdutoMap.produto_id.is_(None))
                 .filter(VndaProdutoMap.ignorar.is_(False)).count())
    sem_loja = (SeruPedidoProcessado.query
                .filter(SeruPedidoProcessado.loja_id.is_(None)).count())
    return lojas + prod_seru + prod_vnda + sem_loja

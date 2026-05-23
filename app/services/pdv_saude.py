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
    from datetime import datetime, time

    from sqlalchemy import func

    from app.constants import VENDA_TIPOS_LOJA
    from app.models import AppConfig, MovEstoqueLoja
    from app.services import seru_cron
    from app.utils import hoje as hoje_brt

    st_seru = seru_cron.status()
    st_vnda = seru_cron.status_vnda()

    # Timestamp persistido (sobrevive a deploy); fallback pro de memoria.
    def _parse_iso(s):
        try:
            return datetime.fromisoformat(s) if s else None
        except (TypeError, ValueError):
            return None

    seru_run = _parse_iso(AppConfig.get('seru_ultimo_sync')) or st_seru.get('ultimo_run')
    vnda_run = _parse_iso(AppConfig.get('vnda_ultimo_sync')) or st_vnda.get('ultimo_run')

    min_seru = _minutos_desde(seru_run)
    min_vnda = _minutos_desde(vnda_run)

    # Itens baixados HOJE (dado real do estoque, por tipo de venda).
    ini_hoje = datetime.combine(hoje_brt(), time.min)
    rows = (MovEstoqueLoja.query
            .filter(MovEstoqueLoja.tipo.in_(VENDA_TIPOS_LOJA))
            .filter(MovEstoqueLoja.data >= ini_hoje)
            .with_entities(MovEstoqueLoja.tipo, func.sum(MovEstoqueLoja.quantidade))
            .group_by(MovEstoqueLoja.tipo).all())
    baixas_hoje = {tipo: int(total or 0) for tipo, total in rows}
    seru_baixados_hoje = baixas_hoje.get('venda_seru', 0)
    vnda_baixados_hoje = baixas_hoje.get('venda_vnda', 0)

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
        'seru_ultimo_run': seru_run,
        'seru_ativo': st_seru.get('ativo'),
        'seru_atrasado': (min_seru is None) or (min_seru > _ATRASO_MIN),
        'seru_baixados_hoje': seru_baixados_hoje,
        'vnda_ultimo_run': vnda_run,
        'vnda_ativo': st_vnda.get('ativo'),
        'vnda_atrasado': (min_vnda is None) or (min_vnda > _ATRASO_MIN),
        'vnda_baixados_hoje': vnda_baixados_hoje,
        'lojas_pendentes': lojas_pendentes,
        'produtos_pendentes_seru': produtos_pendentes_seru,
        'produtos_pendentes_vnda': produtos_pendentes_vnda,
        'pedidos_sem_loja': pedidos_sem_loja,
        'total_pendencias': total_pendencias,
    }


def reconciliar(inicio, fim):
    """Compara vendido no Seru vs baixado no estoque, no periodo [inicio, fim].

    `inicio`/`fim`: date (BRT).

    ATENCAO: nao bate 1:1 por design — produtos com `fator_quantidade` < 1
    (compostos/fatias) e cestas baixam quantidade diferente da vendida. O
    valor real desta tela eh destacar **produtos pendentes vendidos** (esses
    NAO baixaram nada) e dar os totais como ordem de grandeza.

    Retorna dict (ou {'erro': ...} se a API Seru falhar).
    """
    from datetime import datetime, time

    from sqlalchemy import func

    from app.constants import VENDA_TIPOS_LOJA
    from app.models import MovEstoqueLoja
    from app.services import vendas_itens

    try:
        agg = vendas_itens.agregar_itens(inicio, fim)
    except Exception as e:  # noqa: BLE001
        logger.exception('reconciliar: agregar_itens falhou')
        return {'erro': f'{type(e).__name__}: {str(e)[:200]}'}

    produtos = agg.get('produtos', [])
    # Produtos vendidos que NAO baixam (pendentes ou nunca vistos numa sync).
    pendentes_vendidos = [p for p in produtos
                          if p['estado_map'] in ('pendente', 'sem_map')]
    qtd_pendente = sum(p['qtd'] for p in pendentes_vendidos)

    # Produtos com auto-baixa ATIVA (mapeados que venderam no periodo). O
    # sync baixa estoque deles automaticamente. Se o dono tambem baixa
    # algum manualmente (ex: PDV sem API), isso eh DUPLA-BAIXA — ele deve
    # revisar e Ignorar os que controla na mao. Ordena por qtd desc.
    mapeados_vendidos = sorted(
        [p for p in produtos if p['estado_map'] == 'mapeado'],
        key=lambda x: x['qtd'], reverse=True)

    # Baixado no estoque (MovEstoqueLoja) no mesmo periodo, agregado por tipo.
    ini_dt = datetime.combine(inicio, time.min)
    fim_dt = datetime.combine(fim, time.max)
    rows = (MovEstoqueLoja.query
            .filter(MovEstoqueLoja.tipo.in_(VENDA_TIPOS_LOJA))
            .filter(MovEstoqueLoja.data >= ini_dt)
            .filter(MovEstoqueLoja.data <= fim_dt)
            .with_entities(MovEstoqueLoja.tipo,
                           func.sum(MovEstoqueLoja.quantidade))
            .group_by(MovEstoqueLoja.tipo)
            .all())
    movs = {tipo: int(total or 0) for tipo, total in rows}
    baixado_efetivo = movs.get('venda_seru', 0) + movs.get('venda_vnda', 0)
    sem_estoque = (movs.get('venda_seru_sem_estoque', 0)
                   + movs.get('venda_vnda_sem_estoque', 0))

    return {
        'inicio': inicio,
        'fim': fim,
        'seru_total_pedidos': agg.get('total_pedidos', 0),
        'seru_total_itens': agg.get('total_itens_vendidos', 0),
        'seru_faturamento': agg.get('faturamento_total', 0),
        'pendentes_vendidos': pendentes_vendidos,
        'qtd_pendente': qtd_pendente,
        'mapeados_vendidos': mapeados_vendidos,
        'baixado_efetivo': baixado_efetivo,
        'sem_estoque': sem_estoque,
        'movs_por_tipo': movs,
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

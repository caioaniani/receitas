"""Helper para desempacotar Produto-cesta em componentes.

Cesta = um Produto com `ProdutoItem` filhos. Cada componente eh uma
receita ou MP, com quantidade. Exemplo: Family Box = 5 pao + 3 croissant.

Quando uma cesta eh vendida/registrada como saida do estoque, o sistema
DEVE descontar cada componente individualmente — porque a loja so estoca
componentes (ela monta a cesta na hora da venda).

Este helper centraliza a logica que era duplicada em varios servicos:
- vnda_sync.py (delega aqui)
- estoque_loja_lote.py::aplicar_saida_lote
- seru_sync.py
- copilot.py::executar_registrar_desperdicio
- pedidos/routes.py::desperdicio
- pedidos/routes.py::_executar_recebimento_pedido
- pedidos/routes.py::_executar_envio_pedido

DESDE A MIGRATION B5 (efb6e5837fd0): vinculo eh via FK
(`ProdutoItem.receita_id` / `materia_prima_id`). `item_nome` continua
existindo por compat e como fallback humano-legivel. Renomear a receita
NAO quebra mais a cesta — a FK eh estavel.

Itens orfaos (FK NULL — backfill nao resolveu, ou cesta criada via UI
antiga) sao logados com WARNING e ignorados. Admin resolve em
/cestas/orfaos.
"""
import logging

logger = logging.getLogger(__name__)


def componentes_de_cesta(produto):
    """Se produto eh cesta (tem ProdutoItens), retorna lista de
    `(coluna_estoque, id, nome, quantidade_por_unidade_de_cesta)`.

    A coluna_estoque eh 'receita_id', 'produto_id' ou 'materia_prima_id'
    — pronta pra usar como filtro de EstoqueLoja/EstoqueProducao.

    Tipo 'produto' = componente eh outro Produto (ex: iogurte 200ml comprado
    pronto). Loja estoca o produto diretamente. NAO eh expandido recursivamente
    mesmo se o produto-componente tambem fosse cesta — o produto e atomico
    no estoque.

    Se nao for cesta, retorna [].

    ProdutoItem orfao (FK NULL) eh logado e ignorado — admin resolve
    em /produtos/cestas/orfaos.
    """
    if not produto or not produto.itens:
        return []
    out = []
    for pi in produto.itens:
        qtd = float(pi.quantidade or 1.0)
        if pi.tipo == 'receita':
            if pi.receita_id and pi.receita:
                out.append(('receita_id', pi.receita_id, pi.receita.nome, qtd))
            else:
                logger.warning(
                    'ProdutoItem #%s orfao (tipo=receita, item_nome=%r, '
                    'sem receita_id). Componente IGNORADO na baixa de estoque. '
                    'Resolver em /produtos/cestas/orfaos.',
                    pi.id, pi.item_nome,
                )
        elif pi.tipo == 'produto':
            if pi.produto_componente_id and pi.produto_componente:
                out.append(('produto_id', pi.produto_componente_id,
                            pi.produto_componente.nome, qtd))
            else:
                logger.warning(
                    'ProdutoItem #%s orfao (tipo=produto, item_nome=%r, '
                    'sem produto_componente_id). Componente IGNORADO na baixa.',
                    pi.id, pi.item_nome,
                )
        elif pi.tipo == 'mp':
            if pi.materia_prima_id and pi.materia_prima:
                out.append(('materia_prima_id', pi.materia_prima_id,
                            pi.materia_prima.nome, qtd))
            else:
                logger.warning(
                    'ProdutoItem #%s orfao (tipo=mp, item_nome=%r, '
                    'sem materia_prima_id). Componente IGNORADO na baixa.',
                    pi.id, pi.item_nome,
                )
    return out


def composicao_de_venda(*, receita_id=None, produto_id=None,
                        materia_prima_id=None):
    """Composicao de estoque de 1 unidade VENDIDA do item — base do motor unico
    de baixa (`app/services/baixa_venda.py`), que unifica Seru + site + lote.

    - Produto-cesta -> seus componentes (ProdutoItem), via `componentes_de_cesta`
      (ex: Family Box = 5 pao + 3 croissant; sanduiche = 0.2 sourdough + recheio).
    - Produto simples / Receita / MP -> identidade `[(col, id, nome, 1.0)]`.

    Retorna `[(coluna_estoque, item_id, nome, qtd_por_unidade_vendida)]`, pronto
    pra filtrar EstoqueLoja. Lista vazia = nada a baixar (alvo inexistente, ou
    cesta so com orfaos)."""
    if produto_id:
        from app.models import Produto
        produto = Produto.query.get(produto_id)
        if produto is None:
            return []
        comps = componentes_de_cesta(produto)
        if comps:
            return comps
        return [('produto_id', produto_id, produto.nome, 1.0)]
    if receita_id:
        from app.models import Receita
        rec = Receita.query.get(receita_id)
        return [('receita_id', receita_id, rec.nome, 1.0)] if rec else []
    if materia_prima_id:
        from app.models import MateriaPrima
        mp = MateriaPrima.query.get(materia_prima_id)
        return [('materia_prima_id', materia_prima_id, mp.nome, 1.0)] if mp else []
    return []


_COL_PRA_TIPO_CURTO = {
    'receita_id': 'receita',
    'produto_id': 'produto',
    'materia_prima_id': 'mp',
}


def componentes_de_cesta_curto(produto):
    """`componentes_de_cesta` com o tipo no formato CURTO
    ('receita'|'produto'|'mp') em vez do nome da coluna. Usado por
    `loja_online_vendas` na agregacao de vendas do site."""
    return [(_COL_PRA_TIPO_CURTO.get(col, 'mp'), item_id, nome, qtd)
            for col, item_id, nome, qtd in componentes_de_cesta(produto)]


def eh_cesta(produto):
    """True se Produto tem componentes (eh cesta)."""
    return bool(produto and produto.itens)


def contar_produto_itens_orfaos():
    """Conta ProdutoItems orfaos (tipo definido mas FK NULL).

    Usado no dashboard pra mostrar alerta ao owner quando ha cestas
    com componentes nao vinculados — esses componentes nao baixam
    estoque, comportamento detectavel mas silencioso.
    """
    from sqlalchemy import or_

    from app.models import ProdutoItem
    return ProdutoItem.query.filter(
        or_(
            (ProdutoItem.tipo == 'receita') & (ProdutoItem.receita_id.is_(None)),
            (ProdutoItem.tipo == 'produto') & (ProdutoItem.produto_componente_id.is_(None)),
            (ProdutoItem.tipo == 'mp') & (ProdutoItem.materia_prima_id.is_(None)),
        )
    ).count()

"""Helper para desempacotar Produto-cesta em componentes.

Cesta = um Produto com `ProdutoItem` filhos. Cada componente eh uma
receita ou MP, com quantidade. Exemplo: Family Box = 5 pao + 3 croissant.

Quando uma cesta eh vendida/registrada como saida do estoque, o sistema
DEVE descontar cada componente individualmente — porque a loja so estoca
componentes (ela monta a cesta na hora da venda).

Este helper centraliza a logica que era duplicada em varios servicos:
- vnda_sync.py (ja usava _componentes_de_cesta)
- estoque_loja_lote.py::aplicar_saida_lote
- seru_sync.py (faltava)
- copilot.py::executar_registrar_desperdicio (faltava)
- pedidos/routes.py::desperdicio (faltava)
- pedidos/routes.py::_executar_recebimento_pedido (faltava)
- pedidos/routes.py::_executar_envio_pedido (faltava)
"""
from app.models import Receita, MateriaPrima


def componentes_de_cesta(produto):
    """Se produto eh cesta (tem ProdutoItens), retorna lista de
    `(coluna_estoque, id, nome, quantidade_por_unidade_de_cesta)`.

    A coluna_estoque eh 'receita_id' ou 'materia_prima_id' — pronta pra
    usar como filtro de EstoqueLoja/EstoqueProducao.

    Se nao for cesta, retorna [].

    Itens com nome que nao bate exato em Receita/MateriaPrima sao
    silenciosamente ignorados — o admin precisa garantir consistencia
    de nomes via ProdutoItem.item_nome.
    """
    if not produto or not produto.itens:
        return []
    out = []
    for pi in produto.itens:
        qtd = float(pi.quantidade or 1.0)
        if pi.tipo == 'receita':
            r = Receita.query.filter_by(nome=pi.item_nome).first()
            if r:
                out.append(('receita_id', r.id, r.nome, qtd))
        elif pi.tipo == 'mp':
            m = MateriaPrima.query.filter_by(nome=pi.item_nome).first()
            if m:
                out.append(('materia_prima_id', m.id, m.nome, qtd))
    return out


def eh_cesta(produto):
    """True se Produto tem componentes (eh cesta)."""
    return bool(produto and produto.itens)

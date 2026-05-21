"""Constantes de dominio compartilhadas.

Centraliza listas que eram duplicadas (e divergiam) entre services.
Sempre que algum servico filtra "vendas" ou "pedidos finalizados",
deve importar daqui.
"""

# ─── Tipos de movimento de venda ──────────────────────────────────────

# Vendas que baixam de EstoqueLoja (lojas fisicas / e-commerce com retirada)
VENDA_TIPOS_LOJA = (
    'venda_seru', 'venda_seru_sem_estoque',
    'venda_vnda', 'venda_vnda_sem_estoque',
)

# Vendas que baixam de EstoqueProducao (industria / B2B)
VENDA_TIPOS_PRODUCAO = (
    'venda_b2b', 'venda_b2b_sem_estoque',
)

VENDA_TIPOS_TODOS = VENDA_TIPOS_LOJA + VENDA_TIPOS_PRODUCAO


# ─── Status de PedidoLoja ─────────────────────────────────────────────

# Status "terminais" — pedido nao precisa mais aparecer em listas de pendentes.
# 'entregue' eh historico (site usava); 'recebido' eh o novo (copilot).
# Os dois precisam coexistir.
STATUS_PEDIDO_FINALIZADOS = ('entregue', 'recebido', 'cancelado')

# Labels amigaveis pra UI / copilot. Use sempre que precisar mostrar status
# pro usuario final.
STATUS_PEDIDO_LABEL = {
    'pendente': 'pedido feito',
    'confirmado': 'pedido feito',
    'separado': 'enviado',
    'em_transporte': 'enviado',
    'entregue': 'recebido',
    'recebido': 'recebido',
    'cancelado': 'cancelado',
}


# ─── Motivos de desperdicio ────────────────────────────────────────────
#
# `validade` eh especial: se o item (Receita/Produto) tem `reaproveitavel=True`,
# o desperdicio com esse motivo NAO baixa do estoque — o item vence mas
# vira outra coisa (croissant tradicional vira almond, sourdough vira chapa).
# Os outros motivos sempre baixam.

DESPERDICIO_MOTIVOS = ('validade', 'estragou', 'caiu', 'queimou', 'outro')

# Apenas esse motivo respeita a flag `reaproveitavel` do item.
DESPERDICIO_MOTIVO_REAPROVEITAVEL = 'validade'

DESPERDICIO_MOTIVO_LABEL = {
    'validade': 'venceu',
    'estragou': 'estragou',
    'caiu': 'caiu',
    'queimou': 'queimou',
    'outro': 'outro',
}

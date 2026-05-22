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
# Itens (Receita/Produto) com `reaproveitavel=True` NAO baixam estoque
# quando o motivo eh um dos REAPROVEITAVEIS abaixo — vencimento e sobra
# do dia: o item vira outra coisa em vez de virar lixo (croissant tradicional
# vira almond, sourdough vira chapa). Os outros motivos sempre baixam.

DESPERDICIO_MOTIVOS = ('validade', 'nao_vendeu', 'estragou', 'caiu',
                       'queimou', 'outro')

# Motivos que respeitam a flag `reaproveitavel` do item.
DESPERDICIO_MOTIVOS_REAPROVEITAVEIS = ('validade', 'nao_vendeu')

DESPERDICIO_MOTIVO_LABEL = {
    'validade': 'venceu',
    'nao_vendeu': 'nao vendeu / sobra do dia',
    'estragou': 'estragou',
    'caiu': 'caiu',
    'queimou': 'queimou',
    'outro': 'outro',
}


# ─── Estados de produto (familia + estado por item/estoque) ─────────────
#
# Uma Receita pertence a uma familia (`Receita.familia`). A familia
# define quais estados sao possiveis pra essa receita em pedidos/estoque.
# NULL no campo `estado` = "estado padrao da familia" (sem rotulo na UI).
#
# Resumo:
# - viennoiserie: cru (NULL, padrao) / backup (pre-fermentado congelado) /
#   assado (raro — so Nebraska, forno pequeno na loja).
# - pao_sourdough: congelado assado (NULL, unico estado).
# - fornada_especial: assado fresco (NULL, unico estado — focaccia, brioche, etc).

FAMILIAS_RECEITA = ('viennoiserie', 'pao_sourdough', 'fornada_especial')

FAMILIA_LABEL = {
    'viennoiserie': 'Viennoiserie',
    'pao_sourdough': 'Pão / Sourdough',
    'fornada_especial': 'Fornada especial',
}

# Estados validos por familia (alem de NULL = padrao da familia).
# A familia define o que pode aparecer em PedidoItem.estado / EstoqueLoja.estado.
# Pra EstoqueProducao, `assado` nunca eh persistido (industria nao mantem
# vitrine — assa pra cumprir pedido e despacha direto).
ESTADOS_PERMITIDOS = {
    'viennoiserie': ('backup', 'assado'),
    'pao_sourdough': (),
    'fornada_especial': (),
}

# Estados permitidos no EstoqueProducao (subset do ESTADOS_PERMITIDOS).
# Backup eh persistido pra rastreio; assado nao (sai direto).
ESTADOS_PRODUCAO_PERMITIDOS = {
    'viennoiserie': ('backup',),
    'pao_sourdough': (),
    'fornada_especial': (),
}

# Labels amigaveis. Estado NULL renderiza sem tag.
ESTADO_LABEL = {
    'backup': 'BACKUP',
    'assado': 'ASSADO',
}


def familia_default():
    """Familia default pra Receita sem familia setada — assume `pao_sourdough`
    (estado unico, NULL, sem complicacao)."""
    return 'pao_sourdough'


def estados_permitidos_familia(familia):
    """Retorna tupla de estados nao-NULL permitidos pra familia."""
    return ESTADOS_PERMITIDOS.get(familia or familia_default(), ())


def estado_label(estado):
    """Rotulo pra UI/Slack. None ou '' retorna ''. Estado conhecido retorna
    `[TAG]`. Desconhecido retorna `[ESTADO]` cru."""
    if not estado:
        return ''
    return f'[{ESTADO_LABEL.get(estado, estado.upper())}]'


def render_item_com_estado(nome, estado):
    """Concatena nome do item com tag de estado (se houver).
    Ex: ('Croissant Francês', 'backup') -> 'Croissant Francês [BACKUP]'."""
    tag = estado_label(estado)
    return f'{nome} {tag}'.rstrip()

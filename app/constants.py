"""Constantes de dominio compartilhadas.

Centraliza listas que eram duplicadas (e divergiam) entre services.
Sempre que algum servico filtra "vendas" ou "pedidos finalizados",
deve importar daqui.
"""

# ─── Tipos de movimento de venda ──────────────────────────────────────

# Vendas que baixam de EstoqueLoja (lojas fisicas / e-commerce com retirada).
# ATENCAO (02/07/2026): esta lista e a visao de RECONCILIACAO/vigia do ledger
# (pdv_saude, anomalias, loja_pagamento) — NAO e a lista de DEMANDA das
# previsoes. Previsao/reposicao usa VENDA_TIPOS_DEMANDA_LOJA abaixo (inclui
# venda manual e saida_lote, e trata estorno com sinal).
VENDA_TIPOS_LOJA = (
    'venda_seru', 'venda_seru_sem_estoque',
    'venda_vnda', 'venda_vnda_sem_estoque',
    # Loja propria (checkout nativo do site). Baixa so apos 'pago' no
    # webhook do Pagar.me (Fase 4); falta de saldo registra
    # `venda_site_sem_estoque` na propria linha (padrao Seru/VNDA).
    # Estorno (cancelamento ou refund) usa `venda_site_estorno`.
    'venda_site', 'venda_site_sem_estoque', 'venda_site_estorno',
    # PDV do TINY (27/07/2026): a Cantina vende pelo Tiny, nao pelo Seru.
    # Mesma natureza do Seru — venda de balcao que baixa EstoqueLoja.
    'venda_tiny', 'venda_tiny_sem_estoque', 'venda_tiny_estorno',
)

# ─── Demanda de venda da loja (previsoes / ponto de reposicao) ─────────
#
# Unificacao de 02/07/2026 (Fase 0 da revisao dos motores de previsao): eram
# TRES listas divergentes (VENDA_TIPOS_LOJA aqui, _DEMANDA_VENDA_TIPOS em
# previsao_producao.py e SERU_TIPOS em vendas_manuais.py) — cada previsao
# enxergava um total de venda diferente pro mesmo dia/loja.
#
# Tipos que SOMAM demanda de consumo: a baixa real de cada canal + o registro
# de falta (`*_sem_estoque` — a venda ACONTECEU no PDV com o ledger zerado;
# conta como consumo, nao como "demanda extra"). Inclui a venda MANUAL da
# tela de estoque (lojas sem PDV eram sub-contadas) e o legado VNDA (sai da
# janela historica sozinho).
VENDA_TIPOS_DEMANDA_LOJA = (
    'venda_seru', 'venda_seru_sem_estoque',
    'venda_site', 'venda_site_sem_estoque',
    'saida_lote', 'venda_loja_sem_estoque',
    'venda', 'venda_sem_estoque',
    'venda_vnda', 'venda_vnda_sem_estoque',
    # PDV do Tiny (Cantina) — venda de balcao, conta demanda como o Seru.
    'venda_tiny', 'venda_tiny_sem_estoque',
)

# Estornos de venda e o SINAL com que a QUANTIDADE foi GRAVADA no ledger
# (ver baixa_venda._SINAL_ESTORNO): Seru/lote gravam o estorno POSITIVO —
# multiplicar por -1 subtrai da demanda; site (e o legado VNDA, mesma
# convencao; nenhum writer de venda_vnda_estorno foi encontrado no historico
# do repo) grava NEGATIVO — somar direto (sinal +1) ja subtrai. Sem isso,
# venda Seru cancelada contava demanda cheia na media.
VENDA_ESTORNO_SINAL_DEMANDA = {
    'venda_seru_estorno': -1,
    'saida_lote_estorno': -1,
    'venda_site_estorno': 1,
    'venda_vnda_estorno': 1,
    # Tiny grava o estorno POSITIVO (familia Seru/lote) -> sinal -1.
    'venda_tiny_estorno': -1,
}

# Filtro completo pra queries de demanda (vendas + estornos). Agregue com
# `quantidade * VENDA_ESTORNO_SINAL_DEMANDA.get(tipo, 1)`.
VENDA_TIPOS_DEMANDA_COM_ESTORNO = (
    VENDA_TIPOS_DEMANDA_LOJA + tuple(VENDA_ESTORNO_SINAL_DEMANDA))

# Mermas ESTRUTURAIS que a projecao de reposicao da loja trata como consumo
# recorrente: devolucao a industria (ciclo de sobras desejado — croissant
# tradicional devolvido pra virar Almond) e perda (quebra operacional). Sobra/
# descarte/desperdicio ficam FORA de proposito: excesso nao se repoe — incluir
# perpetuaria o proprio desperdicio na sugestao de pedido.
MERMA_TIPOS_PROJECAO = ('devolucao_industria', 'perda')

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

# Pedido finalizado E vendido (chegou na loja) — 'entregue'/'recebido' sem o
# 'cancelado'. Use em faturamento/contagem de entregues: filtrar so 'entregue'
# subconta os pedidos fechados como 'recebido' (copilot). NAO duplicar a dupla.
STATUS_PEDIDO_ENTREGUES = ('entregue', 'recebido')

# Status em que o pedido ainda pode ser EDITADO (itens/qtd/data): depois de
# 'separado' ele ja esta no fluxo fisico — cancela e recria, nao edita.
# Usado pela rota /pedidos/<id>/editar e pela grade de pedidos da semana.
STATUS_PEDIDO_EDITAVEIS = ('pendente', 'confirmado')

# Status cujo estoque da industria AINDA NAO foi baixado. A baixa do
# EstoqueProducao acontece na transicao separado->em_transporte
# (pedidos/routes.py::_executar_envio_pedido). Logo, pedido nestes status e
# demanda "comprometida" que ainda vai consumir o estoque — o balanco de
# producao (previsao_producao.py) usa isso pra nao contar duas vezes o que
# ja saiu da industria.
STATUS_PEDIDO_NAO_BAIXADOS = ('pendente', 'confirmado', 'separado')

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

# Abas da listagem de pedidos, por grupo de status. Ordem = ordem das abas.
# (slug, label, tupla de status que entram na aba).
STATUS_PEDIDO_ABAS = (
    ('pendentes', 'Pendentes/Confirmados', ('pendente', 'confirmado')),
    ('separados', 'Separados', ('separado',)),
    ('em_rota', 'Em rota', ('em_transporte',)),
    ('entregues', 'Entregues', STATUS_PEDIDO_ENTREGUES),
    ('cancelados', 'Cancelados', ('cancelado',)),
)


# ─── Papeis de usuario ────────────────────────────────────────────────
# Validos pra Usuario.papel. Centralizado aqui (estava duplicado em
# auth/routes.py). 'padeiro' = chao de fabrica: tela touchscreen dedicada
# (separar pedido + gerar QR de saida), sem acesso ao resto do sistema.
PAPEIS_VALIDOS = ('admin', 'gerente', 'producao', 'padeiro', 'rh',
                  'marketing', 'observador', 'funcionario')

PAPEL_LABEL = {
    'admin': 'Admin', 'gerente': 'Gerente', 'producao': 'Producao',
    'padeiro': 'Padeiro', 'rh': 'RH', 'marketing': 'Marketing',
    'observador': 'Observador - somente leitura',
    'funcionario': 'Funcionario',
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

# Checklist de loja (03/08/2026): abertura, troca de turno e fechamento.
# O responsável do turno (gerente/atendente chefe) preenche na tela do
# celular; itens cadastráveis pelo dono, foto obrigatória nos marcados.
# 'durante' entrou em 03/08/2026 na importação do checklist em papel: todo
# setor tem um bloco "DURANTE O EXPEDIENTE" (e a Supervisão tem "MEIO DO
# DIA") que não é abertura nem fechamento. 'troca_turno' segue existindo
# separado — é quando o turno realmente muda de responsável.
CHECKLIST_TIPOS = ('abertura', 'durante', 'troca_turno', 'fechamento')

CHECKLIST_TIPO_LABEL = {
    'abertura': 'Abertura',
    'durante': 'Durante o expediente',
    'troca_turno': 'Troca de turno',
    'fechamento': 'Fechamento',
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


# ─── Lalamove ─────────────────────────────────────────────────────────
# Limite mínimo do saldo da carteira Lalamove. Quando o saldo capturado
# pelo webhook WALLET_BALANCE_CHANGED ficar abaixo disso, o dono recebe
# alerta no WhatsApp. Decisão do dono 15/06/2026.
LALAMOVE_SALDO_MIN_REAIS = 200

# Anti-spam do alerta de saldo: não realerta se já avisou nas últimas
# `_ALERTA_DEDUPE_HORAS` E o saldo não caiu pelo menos `_ALERTA_DEDUPE_DELTA`
# (em reais) desde o último alerta. Permite re-alertar se o saldo desabar
# rápido (ex: alertou em R$190, caiu pra R$80 → vale realertar).
LALAMOVE_SALDO_ALERTA_DEDUPE_HORAS = 12
LALAMOVE_SALDO_ALERTA_DEDUPE_DELTA_REAIS = 50


# ── Dados fiscais/legais da padaria ────────────────────────────────────
# Centralizados aqui pra eliminar copy-paste em templates/PDF/services.
# Hoje (22/06/2026) os templates legais ainda usam hardcoded — refatorar
# pra ler daqui quando passar pela proxima rodada de edicao.
PADARIA_RAZAO_SOCIAL = 'O Pão Padaria Artesanal Ltda.'
PADARIA_CNPJ = '40.646.899/0001-39'
PADARIA_ENDERECO = 'Rua Ribeiro do Vale, 455 — Brooklin Paulista, São Paulo/SP, CEP 04568-001'
# Chave PIX = CNPJ. Mesmo formato exibido pra o cliente; o PSP normaliza
# os pontos/barra ao processar.
PADARIA_CHAVE_PIX = '40.646.899/0001-39'
PADARIA_PIX_TIPO = 'CNPJ'  # rotulo legivel ("Chave PIX (CNPJ): ...")


# ── Etapas de produção padrão por categoria (seed do Gantt) ──────────────────
# Cada etapa: (nome, duracao_min, equipamento, ativa).
#  - equipamento: 'amassadeira'/'forno'/'bancada'/'camara_fria'/None. Os que
#    usam equipamento serializam (a padaria tem 1 de cada).
#  - ativa=False = etapa PASSIVA (fermentação/descanso longo) — acontece entre
#    turnos, não ocupa mão-de-obra no Gantt.
# Tempos típicos artesanais (pesquisados); o dono ajusta por receita depois.
ETAPAS_PADRAO = {
    'Pães': [
        ('Mise en place', 10, None, True),
        ('Conferência', 5, None, True),
        ('Autólise', 30, None, False),
        ('Amassamento', 15, 'amassadeira', True),
        # Fermentação em bloco com dobras: mais descanso que mão de obra — passiva
        # (o padeiro só dobra rápido a cada 30 min, fica livre pra outra receita).
        ('Bulk + dobras', 120, None, False),
        ('Pré-modelagem', 10, 'bancada', True),
        ('Modelagem', 15, 'bancada', True),
        ('Fermentação final (frio)', 2880, 'camara_fria', False),
        ('Forno (com vapor)', 25, 'forno', True),
    ],
    'Viennoiserie': [
        ('Mise en place', 10, None, True),
        ('Amassamento', 12, 'amassadeira', True),
        ('Descanso a frio', 720, 'camara_fria', False),
        ('Laminagem (3 dobras)', 150, 'bancada', True),
        ('Modelagem', 20, 'bancada', True),
        ('Fermentação final', 90, None, False),
        ('Forno', 18, 'forno', True),
    ],
}

ETAPAS_PADRAO_DEFAULT = [
    ('Mise en place', 10, None, True),
    ('Preparo', 30, 'bancada', True),
    ('Forno', 20, 'forno', True),
]


def etapas_padrao_categoria(categoria):
    """Etapas padrão da categoria (fallback no default genérico)."""
    return ETAPAS_PADRAO.get((categoria or '').strip(), ETAPAS_PADRAO_DEFAULT)


# Canais Seru de DELIVERY cujos pedidos chegam SEM itens por natureza da
# integracao (o app manda so o total). Decisao do dono 18/07/2026: sao venda
# REAL — contam no faturamento do /pdv/ (rodape so informa que nao baixam
# estoque) e NAO disparam o vigia de venda sem item (rotina, nao suspeita).
# Cobranca sem item de canal FORA desta lista (PDV Facil, desconhecidos) e
# a classe suspeita: fica fora do faturamento e alerta o dono.
SEM_ITENS_CANAIS_DELIVERY = {'99food', 'ifood', 'rappi'}

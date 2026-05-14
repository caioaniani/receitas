"""Servico do Copilot: interpreta comandos em linguagem natural via
Claude Haiku 4.5 e retorna acoes estruturadas pra preview/aprovacao.

Tools suportadas:
- criar_pedido (write) — pedido de loja pra producao
- consultar_pedido (read) — consulta pedidos por cliente/data/status
- consultar_estoque (read) — consulta estoque MP / produtos
- receber_mp (write) — entrada de materia-prima
- ajuste_estoque (write) — ajuste manual (quebra/perda)
- registrar_desperdicio (write) — registra sobra do dia/vencido na loja
- consultar_desperdicio (read) — lista desperdicios por periodo

Toda acao 'write' retorna preview pra aprovacao manual antes de executar.
Acoes 'read' executam direto e retornam texto."""
import json
import logging
import os
from datetime import date, datetime, timedelta

from flask import current_app

from app.extensions import db
from app.utils import agora, hoje
from app.models import Loja, Produto, Receita, MateriaPrima

logger = logging.getLogger(__name__)


# ── Tools ──────────────────────────────────────────────────────────────

TOOL_CRIAR_PEDIDO = {
    "name": "criar_pedido",
    "description": (
        "Cria um pedido de loja pra producao. Use quando o usuario pedir pra "
        "encomendar/pedir/solicitar produtos pra uma data. NAO executa direto — "
        "retorna preview pra usuario confirmar."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "loja_id": {"type": ["integer", "null"], "description": "ID da loja destinataria. Null se nao mencionada."},
            "data_entrega": {"type": "string", "description": "Data YYYY-MM-DD. Resolva 'amanha'/'sexta'/etc."},
            "itens": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "nome": {"type": "string", "description": "Nome EXATO do produto/receita do catalogo."},
                        "quantidade": {"type": "integer", "minimum": 1},
                    },
                    "required": ["nome", "quantidade"],
                },
                "minItems": 1,
            },
            "observacao": {"type": ["string", "null"]},
        },
        "required": ["data_entrega", "itens"],
    },
}

TOOL_CONSULTAR_PEDIDO = {
    "name": "consultar_pedido",
    "description": "Consulta pedidos por loja, data, status ou ID. Retorna lista de pedidos.",
    "input_schema": {
        "type": "object",
        "properties": {
            "loja_id": {"type": ["integer", "null"]},
            "data_de": {"type": ["string", "null"], "description": "Data inicial YYYY-MM-DD."},
            "data_ate": {"type": ["string", "null"], "description": "Data final YYYY-MM-DD."},
            "status": {"type": ["string", "null"], "enum": [None, "pendente", "confirmado", "separado", "em_transporte", "entregue", "cancelado"]},
            "pedido_id": {"type": ["integer", "null"]},
        },
    },
}

TOOL_CONSULTAR_ESTOQUE = {
    "name": "consultar_estoque",
    "description": "Consulta estoque atual de uma materia-prima especifica, ou lista MPs com estoque baixo.",
    "input_schema": {
        "type": "object",
        "properties": {
            "mp_nome": {"type": ["string", "null"], "description": "Nome EXATO da MP. Null pra listar todas baixas."},
            "apenas_baixo": {"type": "boolean", "default": False, "description": "Se true, lista so MPs abaixo do estoque minimo."},
        },
    },
}

TOOL_RECEBER_MP = {
    "name": "receber_mp",
    "description": "Registra entrada de materia-prima no estoque. NAO executa direto — retorna preview.",
    "input_schema": {
        "type": "object",
        "properties": {
            "mp_nome": {"type": "string", "description": "Nome EXATO da MP do catalogo."},
            "quantidade": {"type": "number", "minimum": 0.01},
            "preco_total": {"type": ["number", "null"], "description": "Valor total pago (R$). Calcula preco_unitario automaticamente."},
            "preco_unitario": {"type": ["number", "null"], "description": "Preco por unidade. Alternativa ao preco_total."},
            "referencia": {"type": ["string", "null"], "description": "Ex: 'NF 12345 - Fornecedor X'."},
        },
        "required": ["mp_nome", "quantidade"],
    },
}

TOOL_AJUSTE_ESTOQUE = {
    "name": "ajuste_estoque",
    "description": "Ajuste manual de estoque MP (quebra, perda, contagem, devolucao). NAO executa direto — preview.",
    "input_schema": {
        "type": "object",
        "properties": {
            "mp_nome": {"type": "string"},
            "quantidade": {"type": "number", "minimum": 0.01, "description": "Sempre positiva. tipo decide se entra ou sai."},
            "tipo": {"type": "string", "enum": ["entrada", "saida"]},
            "motivo": {"type": "string", "description": "Ex: 'quebra por umidade', 'contagem fisica', 'devolucao'."},
        },
        "required": ["mp_nome", "quantidade", "tipo", "motivo"],
    },
}

# ───── Tools novas — acoes ─────────────────────────────────────────────

TOOL_MUDAR_STATUS_PEDIDO = {
    "name": "mudar_status_pedido",
    "description": "Confirma, separa, envia ou cancela um pedido entre lojas. NAO executa — preview.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pedido_id": {"type": "integer"},
            "novo_status": {"type": "string", "enum": ["confirmar", "separar", "enviar", "cancelar"]},
        },
        "required": ["pedido_id", "novo_status"],
    },
}

TOOL_CRIAR_FORNECEDOR = {
    "name": "criar_fornecedor",
    "description": "Cadastra novo fornecedor. NAO executa — preview.",
    "input_schema": {
        "type": "object",
        "properties": {
            "nome": {"type": "string"},
            "cnpj": {"type": ["string", "null"]},
            "telefone": {"type": ["string", "null"]},
            "email": {"type": ["string", "null"]},
            "contato": {"type": ["string", "null"], "description": "Nome do vendedor"},
            "observacao": {"type": ["string", "null"]},
        },
        "required": ["nome"],
    },
}

TOOL_MARCAR_PONTO = {
    "name": "marcar_ponto",
    "description": "Marca ponto de funcionario (entrada/saida). Use quando alguem diz 'marca ponto do Joao' ou similar.",
    "input_schema": {
        "type": "object",
        "properties": {
            "funcionario_nome": {"type": "string"},
            "tipo": {"type": "string", "enum": ["entrada", "saida", "saida_almoco", "volta_almoco"]},
        },
        "required": ["funcionario_nome", "tipo"],
    },
}

TOOL_CRIAR_TAREFA = {
    "name": "criar_tarefa",
    "description": "Cria tarefa em projetos. Inbox por default; pra projeto especifico, passe projeto_nome.",
    "input_schema": {
        "type": "object",
        "properties": {
            "titulo": {"type": "string"},
            "projeto_nome": {"type": ["string", "null"]},
            "data_prazo": {"type": ["string", "null"], "description": "YYYY-MM-DD se mencionado"},
            "responsavel_nome": {"type": ["string", "null"]},
        },
        "required": ["titulo"],
    },
}

# ───── Tools novas — consultas (read) ──────────────────────────────────

TOOL_CONSULTAR_FORNECEDORES = {
    "name": "consultar_fornecedores",
    "description": "Lista fornecedores cadastrados. Filtros opcionais.",
    "input_schema": {
        "type": "object",
        "properties": {
            "busca": {"type": ["string", "null"], "description": "Filtra por nome"},
            "apenas_ativos": {"type": "boolean", "default": True},
        },
    },
}

TOOL_CONSULTAR_MARGEM = {
    "name": "consultar_margem",
    "description": "Retorna custo + preco + margem de uma receita ou produto.",
    "input_schema": {
        "type": "object",
        "properties": {
            "nome": {"type": "string", "description": "Nome exato da receita/produto"},
        },
        "required": ["nome"],
    },
}

TOOL_CONSULTAR_FUNCIONARIO = {
    "name": "consultar_funcionario",
    "description": "Info de funcionario por nome. Loja, função, salario, status.",
    "input_schema": {
        "type": "object",
        "properties": {
            "nome": {"type": "string"},
        },
        "required": ["nome"],
    },
}

TOOL_CONSULTAR_CAIXA = {
    "name": "consultar_caixa",
    "description": "Numeros do dia: pedidos locais, entregas, compras de MP. Local — NAO inclui PDV/VNDA.",
    "input_schema": {
        "type": "object",
        "properties": {
            "data": {"type": ["string", "null"], "description": "YYYY-MM-DD. Default hoje."},
        },
    },
}

TOOL_CONSULTAR_VENDAS_ITENS = {
    "name": "consultar_vendas_itens",
    "description": (
        "Vendas POR PRODUTO no PDV/Seru. Use quando o usuario perguntar "
        "'o que mais vendeu', 'top produtos', 'vendas de X no periodo', "
        "'quanto saiu de Y na loja Z'. Retorna lista de produtos com "
        "quantidade vendida, faturamento e % do total. Pode filtrar por "
        "loja Seru (nome exato como aparece la — passe a string igual "
        "o usuario disser e o sistema vai listar as opcoes se nao bater)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "inicio": {"type": ["string", "null"], "description": "YYYY-MM-DD. Default hoje."},
            "fim": {"type": ["string", "null"], "description": "YYYY-MM-DD. Default hoje."},
            "loja": {"type": ["string", "null"], "description": "Nome da loja na Seru (opcional)."},
            "top": {"type": ["integer", "null"], "description": "Quantos produtos retornar (default 10, max 30)."},
        },
    },
}

# ───── Tools de Planejamento (PARA + 12 Week Year) ─────────────────────

TOOL_CONSULTAR_FOCO = {
    "name": "consultar_foco",
    "description": "Lista projetos marcados como FOCO das 12 semanas + tarefas pendentes deles. Use quando alguem pergunta 'no que estou focado?', 'qual o foco?', 'meu planejamento'.",
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}

TOOL_CONSULTAR_TAREFAS = {
    "name": "consultar_tarefas",
    "description": "Lista tarefas com filtros. Use pra 'quais tarefas pendentes?', 'tarefas atrasadas', 'tarefas do projeto X'.",
    "input_schema": {
        "type": "object",
        "properties": {
            "projeto_nome": {"type": ["string", "null"]},
            "apenas_atrasadas": {"type": "boolean", "default": False},
            "apenas_pendentes": {"type": "boolean", "default": True},
            "apenas_foco": {"type": "boolean", "default": False, "description": "So tarefas de projetos foco_12s"},
        },
    },
}

TOOL_MARCAR_TAREFA_FEITA = {
    "name": "marcar_tarefa_feita",
    "description": "Marca tarefa como concluida (status='feito'). Preview antes.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tarefa_id": {"type": "integer"},
        },
        "required": ["tarefa_id"],
    },
}

TOOL_BALANCO_CONGELADOS = {
    "name": "balanco_congelados",
    "description": (
        "Lanca balanco/inventario do estoque de congelados (sobrescreve as "
        "quantidades pra bater com a contagem fisica). Use quando o usuario "
        "disser 'fazer balanco de congelados', 'lancar inventario', 'corrigir "
        "estoque do freezer', ou ditar uma lista de itens com quantidades "
        "absolutas pra setar o estoque. NAO usar pra 'entrada de producao' "
        "(que SOMA) ou 'perda/quebra' (que SUBTRAI) — esses tem tools/rotas "
        "proprias. NAO executa direto — retorna preview pra aprovar."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "itens": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "nome": {"type": "string", "description": "Nome do produto/receita. Aceita aproximacoes (sistema faz fuzzy match)."},
                        "quantidade": {"type": "integer", "minimum": 0, "description": "Quantidade CONTADA fisicamente (valor absoluto, nao delta)."},
                    },
                    "required": ["nome", "quantidade"],
                },
                "minItems": 1,
            },
            "referencia": {"type": ["string", "null"], "description": "Identificacao do balanco. Ex: 'Inventario 13/05', 'Contagem semanal'."},
        },
        "required": ["itens"],
    },
}

TOOL_ENTRADA_LOTE_LOJA = {
    "name": "entrada_lote_loja",
    "description": (
        "Lanca entrada em lote no estoque de uma loja especifica (SOMA as "
        "quantidades ao estoque atual). Use quando o usuario disser 'dar "
        "entrada na loja X', 'chegou entrega na loja Y', 'somar no estoque "
        "da loja Z' com uma lista de itens. Itens sem cadastro no sistema "
        "entram como pendentes e depois o admin vincula. NAO usar pra "
        "balanco (sobrescrever) ou pra estoque de congelados (que e outra "
        "tool). NAO executa direto — retorna preview pra aprovar."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "loja_id": {"type": "integer", "description": "ID da loja onde sera lancada a entrada."},
            "itens": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "nome": {"type": "string", "description": "Nome do item. Aceita aproximacoes (fuzzy match)."},
                        "quantidade": {"type": "integer", "minimum": 1, "description": "Quantidade a SOMAR no estoque atual."},
                    },
                    "required": ["nome", "quantidade"],
                },
                "minItems": 1,
            },
            "referencia": {"type": ["string", "null"], "description": "Ex: 'Entrega 13/05'."},
        },
        "required": ["loja_id", "itens"],
    },
}

TOOL_REGISTRAR_DESPERDICIO = {
    "name": "registrar_desperdicio",
    "description": "Registra sobra do dia descartada na loja (vencido/estragado/queimado/caiu). Baixa do estoque. NAO executa — preview.",
    "input_schema": {
        "type": "object",
        "properties": {
            "loja_id": {"type": ["integer", "null"]},
            "loja_nome": {"type": ["string", "null"], "description": "Nome da loja. Use loja_id quando souber."},
            "item_nome": {"type": "string", "description": "Nome da receita, produto ou MP descartado."},
            "quantidade": {"type": "integer", "minimum": 1},
            "motivo": {"type": "string", "enum": ["vencido", "estragado", "queimado", "caiu", "outro"], "description": "Default 'vencido'."},
            "observacao": {"type": ["string", "null"]},
        },
        "required": ["item_nome", "quantidade"],
    },
}

TOOL_CONSULTAR_DESPERDICIO = {
    "name": "consultar_desperdicio",
    "description": "Lista desperdicios registrados num periodo (default ultimos 7 dias). Filtra por loja opcionalmente.",
    "input_schema": {
        "type": "object",
        "properties": {
            "loja_nome": {"type": ["string", "null"]},
            "dias": {"type": "integer", "minimum": 1, "maximum": 90, "description": "Janela de dias. Default 7."},
        },
    },
}

TOOLS = [
    # Existentes
    TOOL_CRIAR_PEDIDO, TOOL_CONSULTAR_PEDIDO, TOOL_CONSULTAR_ESTOQUE,
    TOOL_RECEBER_MP, TOOL_AJUSTE_ESTOQUE,
    # Novas — acoes operacionais
    TOOL_MUDAR_STATUS_PEDIDO, TOOL_CRIAR_FORNECEDOR, TOOL_MARCAR_PONTO,
    TOOL_CRIAR_TAREFA,
    # Novas — consultas
    TOOL_CONSULTAR_FORNECEDORES, TOOL_CONSULTAR_MARGEM,
    TOOL_CONSULTAR_FUNCIONARIO, TOOL_CONSULTAR_CAIXA,
    TOOL_CONSULTAR_VENDAS_ITENS,
    # Planejamento
    TOOL_CONSULTAR_FOCO, TOOL_CONSULTAR_TAREFAS, TOOL_MARCAR_TAREFA_FEITA,
    # Estoque de congelados / loja
    TOOL_BALANCO_CONGELADOS, TOOL_ENTRADA_LOTE_LOJA,
    # Desperdicio (sobra do dia / vencido)
    TOOL_REGISTRAR_DESPERDICIO, TOOL_CONSULTAR_DESPERDICIO,
]

# Quais tools requerem preview/aprovacao (writes)
REQUER_APROVACAO = {
    'criar_pedido', 'receber_mp', 'ajuste_estoque',
    'mudar_status_pedido', 'criar_fornecedor', 'marcar_ponto', 'criar_tarefa',
    'marcar_tarefa_feita', 'balanco_congelados', 'entrada_lote_loja',
    'registrar_desperdicio',
}


# ── PERMISSOES POR TOOL ────────────────────────────────────────────────
# Tres papeis: 'admin' (tudo), 'gerente' (loja), 'funcionario' (limitado).
# Cada tool lista quais papeis podem usar. Default (nao listado) = so admin.
PAPEIS_POR_TOOL = {
    # Operacao geral — admin + gerente
    'criar_pedido': {'admin', 'gerente'},
    'mudar_status_pedido': {'admin', 'gerente'},
    'receber_mp': {'admin', 'gerente'},
    'ajuste_estoque': {'admin', 'gerente'},
    'marcar_ponto': {'admin', 'gerente'},
    # Cadastros — so admin
    'criar_fornecedor': {'admin'},
    'consultar_margem': {'admin'},
    # Balanco de congelados — so admin (sobrescreve estoque)
    'balanco_congelados': {'admin'},
    # Entrada em lote no estoque de loja — so admin
    'entrada_lote_loja': {'admin'},
    # Consultas operacionais — admin + gerente
    'consultar_fornecedores': {'admin', 'gerente'},
    'consultar_funcionario': {'admin', 'gerente'},
    'consultar_caixa': {'admin', 'gerente'},
    'consultar_vendas_itens': {'admin', 'gerente'},
    'consultar_desperdicio': {'admin', 'gerente'},
    'registrar_desperdicio': {'admin', 'gerente'},
    # Consultas + planejamento — todos
    'consultar_pedido': {'admin', 'gerente', 'funcionario'},
    'consultar_estoque': {'admin', 'gerente', 'funcionario'},
    'consultar_foco': {'admin', 'gerente', 'funcionario'},
    'consultar_tarefas': {'admin', 'gerente', 'funcionario'},
    'criar_tarefa': {'admin', 'gerente', 'funcionario'},
    'marcar_tarefa_feita': {'admin', 'gerente', 'funcionario'},
}


def papel_efetivo(user):
    """Mapeia user → string de papel canonico pra checagem."""
    if not user or not getattr(user, 'is_authenticated', False):
        return None
    if user.is_admin():
        return 'admin'
    papel = (getattr(user, 'papel', None) or '').lower()
    if papel == 'gerente':
        return 'gerente'
    return 'funcionario'


def pode_usar(tool_name, user):
    papel = papel_efetivo(user)
    if not papel:
        return False
    permitidos = PAPEIS_POR_TOOL.get(tool_name, {'admin'})
    return papel in permitidos


def tools_permitidas(user):
    """Lista tools que o user pode usar — vai pro Claude no system prompt
    pra ele nao tentar tools que vao ser rejeitadas."""
    return [t for t in TOOLS if pode_usar(t['name'], user)]


def _catalogo_texto():
    """Lista produtos + receitas + MPs + fornecedores + funcionarios
    formatados pra contexto do LLM. Crescera com o sistema; cache de
    prompt da Anthropic mantem custo baixo."""
    from app.models import Fornecedor, Funcionario
    linhas = ["PRODUTOS (use o nome exato):"]
    for p in Produto.query.order_by(Produto.nome).all():
        linhas.append(f"  - {p.nome}")
    linhas.append("")
    linhas.append("RECEITAS (use o nome exato):")
    for r in Receita.query.order_by(Receita.nome).all():
        linhas.append(f"  - {r.nome}")
    linhas.append("")
    linhas.append("MATERIAS PRIMAS (use o nome exato):")
    for m in MateriaPrima.query.order_by(MateriaPrima.nome).all():
        unidade = m.unidade or '?'
        linhas.append(f"  - {m.nome} ({unidade})")
    linhas.append("")
    linhas.append("FORNECEDORES ATIVOS:")
    for f in Fornecedor.query.filter_by(ativo=True).order_by(Fornecedor.nome).all():
        linhas.append(f"  - {f.nome}")
    if not Fornecedor.query.filter_by(ativo=True).first():
        linhas.append("  (nenhum cadastrado)")
    linhas.append("")
    linhas.append("FUNCIONARIOS ATIVOS:")
    funcs = (Funcionario.query.filter_by(ativo=True)
             .order_by(Funcionario.nome).limit(80).all())
    for f in funcs:
        funcao = f.funcao or f.funcao_operacional or '?'
        linhas.append(f"  - {f.nome} ({funcao})")
    if not funcs:
        linhas.append("  (nenhum cadastrado)")
    return "\n".join(linhas)


def _lojas_texto(user):
    if user.is_admin():
        lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    else:
        lojas = [user.loja] if user.loja_id and user.loja else []
    if not lojas:
        return "(nenhuma loja disponivel)"
    return "\n".join(f"  - id={l.id}: {l.nome}" for l in lojas)


def _build_system_prompt(user):
    hoje = date.today().isoformat()
    return f"""Voce e' um assistente de gestao de uma padaria. Interpreta comandos em
linguagem natural e estrutura acoes pra o usuario confirmar.

Hoje e' {hoje}.

LOJAS DISPONIVEIS:
{_lojas_texto(user)}

{_catalogo_texto()}

TOOLS DISPONIVEIS — ACOES:
- criar_pedido: criar encomenda de produtos pra producao entregar numa loja
- mudar_status_pedido: confirmar / separar / enviar / cancelar pedido (use pedido_id)
- receber_mp: registrar entrada de materia-prima (compra/fornecedor)
- ajuste_estoque: quebra, perda, contagem fisica de MP
- criar_fornecedor: cadastrar novo fornecedor
- marcar_ponto: registrar ponto de funcionario (entrada, saida, almoco)
- criar_tarefa: criar tarefa em projetos (inbox ou projeto especifico)
- balanco_congelados: balanco/inventario do estoque de congelados (SOBRESCREVE quantidades). Use quando o usuario ditar uma contagem fisica do freezer — valores absolutos, nao deltas. Diferente de 'entrada de producao' (que soma).
- entrada_lote_loja: entrada em lote no estoque de uma LOJA especifica (SOMA quantidades). Use quando o usuario disser 'dar entrada na loja X', 'chegou entrega na loja Y', 'somar no estoque da loja Z' + lista de itens. Precisa do loja_id — se nao souber qual loja, pergunte primeiro. Itens sem cadastro entram como pendentes.
- registrar_desperdicio: sobra do dia / vencido / estragado / queimado / caiu — baixa do estoque da loja. Use quando o usuario disser 'venceu X', 'descartei Y', 'sobrou no balcao'. Default motivo='vencido'. **SEMPRE preencha `loja_nome` com o que o usuario falou** (ex: 'nebraska', 'anesio'). O servidor faz fuzzy match com a lista de lojas. Se o usuario NAO mencionou loja e o user logado nao tem loja padrao, pergunte qual loja antes de chamar a tool.

TOOLS DISPONIVEIS — CONSULTAS (read, sem aprovacao):
- consultar_pedido: ver pedidos por loja/data/status/id
- consultar_estoque: estoque de MP especifica ou MPs em alerta
- consultar_fornecedores: lista fornecedores
- consultar_margem: custo + preco + margem de receita/produto
- consultar_funcionario: info de funcionario
- consultar_caixa: numeros do dia (entregas, pedidos locais, compras MP)
- consultar_vendas_itens: vendas POR PRODUTO no PDV/Seru no intervalo (top N + filtro de loja). Use pra 'o que mais vendeu', 'top produtos', 'quanto saiu de X'.
- consultar_desperdicio: lista desperdicios (sobra do dia) por periodo + loja. Use pra 'quanto venceu', 'desperdicio da semana', 'sobrou de X'.
- consultar_foco: lista projetos foco_12s + tarefas pendentes deles
- consultar_tarefas: lista tarefas com filtros (atrasadas, pendentes, projeto, foco)

TOOLS DISPONIVEIS — PLANEJAMENTO (PARA + 12 Week Year):
- marcar_tarefa_feita: marca uma tarefa como concluida (preview)
- criar_tarefa: cria nova tarefa em projeto ou inbox

REGRAS:
- Use o nome EXATO dos catalogos. Se ambiguo ('100 croissants' com varios tipos),
  escolha o mais provavel e mencione na sua resposta-texto que o usuario confirme.
- Datas relativas: resolva 'amanha', 'sexta', 'segunda', etc. pra YYYY-MM-DD.
- Pra criar_pedido sem loja mencionada (admin), use loja_id=null e explique
  que o usuario precisa escolher no preview.
- Pra mudar_status_pedido, se o usuario nao mencionar id explicitamente
  ('marca o pedido de hoje como entregue'), CONSULTE primeiro com
  consultar_pedido pra achar e pergunte 'quer mudar o status do pedido #X?'.
- marcar_ponto: se nao especificar tipo, assuma 'entrada' se for cedo (<13h)
  ou 'saida' senao. Sempre mencione no texto qual escolheu.
- BALANCO_CONGELADOS — REGRA CRITICA: quando o usuario ditar uma LISTA de
  itens com quantidades absolutas (ex: 'pao frances 570, croissant 2060,
  cookie 718...') OU mencionar 'balanco', 'inventario', 'contagem fisica',
  'corrigir estoque' — CHAME A TOOL balanco_congelados IMEDIATAMENTE,
  no MESMO turno, com TODOS os itens citados. NUNCA escreva 'vou preparar
  o preview' ou 'estou estruturando' em texto — a tool gera o preview
  automaticamente pra aprovacao. Mapeie cada nome ditado pro nome EXATO
  do catalogo de RECEITAS (priorize produtos prontos/assados; NUNCA escolha
  'Massa de X' quando o usuario disse 'X' simples — massa e materia prima,
  nao produto final). Itens que voce nao reconhecer no catalogo: passe
  o nome como ditado — o sistema grava como 'pendente' e o usuario vincula
  a uma receita depois em /pedidos/congelados.
- Se algo for impossivel ou ambiguo demais, NAO use tool — responda em texto
  pedindo clarificacao.
- Respostas: portugues brasileiro, lowercase, conciso (1-2 frases).
"""


def interpretar(prompt_text, user, historico=None, images=None):
    """Chama Claude. Retorna dict com tipo, params, explicacao.

    historico: lista de {role: 'user'|'assistant', content: str} com
    conversas anteriores nessa sessao. Permite copilot lembrar contexto
    ('ah entendi, foi aqui' depois de uma resposta).

    images: lista opcional de {mimetype, base64} pra mandar imagens junto
    com o prompt (vision do Haiku). Usado pelo Slack bot.
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY') or current_app.config.get('ANTHROPIC_API_KEY')
    if not api_key:
        return {'tipo': 'erro', 'explicacao': 'Copilot indisponivel: ANTHROPIC_API_KEY nao configurada.', 'raw': None}
    try:
        import anthropic
    except ImportError:
        return {'tipo': 'erro', 'explicacao': 'Biblioteca anthropic nao instalada.', 'raw': None}

    client = anthropic.Anthropic(api_key=api_key)
    system = _build_system_prompt(user)

    # Monta mensagens: historico (filtrado/sanitizado) + prompt atual
    messages = []
    for m in (historico or []):
        role = m.get('role')
        content = (m.get('content') or '').strip()
        if role in ('user', 'assistant') and content:
            messages.append({'role': role, 'content': content})

    # Se tem imagens, prompt vira lista de content blocks
    if images:
        content_blocks = []
        for img in images[:5]:  # limita 5 imagens por msg
            content_blocks.append({
                'type': 'image',
                'source': {
                    'type': 'base64',
                    'media_type': img.get('mimetype') or 'image/jpeg',
                    'data': img.get('base64') or '',
                },
            })
        content_blocks.append({'type': 'text', 'text': prompt_text})
        messages.append({'role': 'user', 'content': content_blocks})
    else:
        messages.append({'role': 'user', 'content': prompt_text})
    # Limita as ultimas 20 mensagens pra nao estourar tokens
    if len(messages) > 20:
        messages = messages[-20:]
        # Garante que comece com user (Claude exige primeira msg = user)
        while messages and messages[0]['role'] != 'user':
            messages = messages[1:]

    # Filtra tools pelo papel do user — Claude so ve o que o user pode usar
    tools_filtradas = tools_permitidas(user)
    if not tools_filtradas:
        return {'tipo': 'erro', 'explicacao': 'Sem permissao pra usar o copilot.', 'raw': None}

    try:
        response = client.messages.create(
            model='claude-haiku-4-5',
            max_tokens=4000,
            system=[{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}],
            tools=tools_filtradas,
            messages=messages,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception('Copilot: erro Anthropic')
        return {'tipo': 'erro', 'explicacao': f'Erro Anthropic: {exc}', 'raw': None}

    tool_call = None
    tool_name = None
    texto_partes = []
    for block in response.content:
        if block.type == 'tool_use':
            tool_call = block.input
            tool_name = block.name
        elif block.type == 'text':
            texto_partes.append(block.text)

    explicacao = ' '.join(texto_partes).strip() or '(sem comentario do copilot)'
    raw = {
        'stop_reason': response.stop_reason,
        'usage': {
            'input': response.usage.input_tokens, 'output': response.usage.output_tokens,
            'cache_read': getattr(response.usage, 'cache_read_input_tokens', 0),
            'cache_create': getattr(response.usage, 'cache_creation_input_tokens', 0),
        },
    }

    if tool_call and tool_name:
        # Enriquece params com info do banco (matches de produto/MP)
        params = _enriquecer_params(tool_name, tool_call, user)
        # Tools 'read' executam direto agora e retornam resultado no campo `resultado`
        if tool_name not in REQUER_APROVACAO:
            resultado = _executar_read(tool_name, params, user)
            return {
                'tipo': tool_name, 'params': params, 'explicacao': explicacao,
                'resultado': resultado, 'requer_aprovacao': False, 'raw': raw,
            }
        return {
            'tipo': tool_name, 'params': params, 'explicacao': explicacao,
            'requer_aprovacao': True, 'raw': raw,
        }
    return {'tipo': 'conversa', 'explicacao': explicacao, 'raw': raw}


def _enriquecer_params(tool_name, tool_input, user):
    """Adiciona matches do banco aos params pra o preview poder mostrar opcoes."""
    if tool_name == 'criar_pedido':
        return _enriquecer_criar_pedido(tool_input)
    if tool_name in ('receber_mp', 'ajuste_estoque'):
        nome = (tool_input.get('mp_nome') or '').strip()
        matches = _resolver_mp(nome) if nome else []
        return {**tool_input, 'mp_matches': matches, 'mp_resolvida': matches[0] if matches else None}
    if tool_name == 'balanco_congelados':
        return _enriquecer_balanco_congelados(tool_input)
    if tool_name == 'entrada_lote_loja':
        return _enriquecer_entrada_lote_loja(tool_input)
    if tool_name == 'registrar_desperdicio':
        return _enriquecer_registrar_desperdicio(tool_input, user)
    # consultar_pedido / consultar_estoque: passam direto
    return tool_input


def _enriquecer_registrar_desperdicio(tool_input, user):
    """Resolve loja_nome + item_nome no banco antes do preview."""
    from sqlalchemy import func
    out = dict(tool_input)
    # Loja: tenta id, depois nome
    loja = None
    try:
        loja_id = int(out.get('loja_id') or 0) or None
    except (TypeError, ValueError):
        loja_id = None
    if loja_id:
        loja = Loja.query.get(loja_id)
    if not loja:
        nome = (out.get('loja_nome') or '').strip()
        if nome:
            loja = Loja.query.filter(func.lower(Loja.nome) == nome.lower()).first()
            if not loja:
                loja = Loja.query.filter(Loja.nome.ilike(f'%{nome}%')).first()
    if loja:
        out['loja_id'] = loja.id
        out['loja_nome'] = loja.nome
    # Item: ja resolvido em executor via _resolver_item_qualquer
    return out


def _enriquecer_balanco_congelados(tool_input):
    """Resolve cada item e adiciona match + estoque atual + delta pra preview."""
    from app.services import estoque_congelados as svc
    itens_raw = tool_input.get('itens') or []
    # Adapta pro formato do servico: precisa de 'linha' fake
    parseados = [{'linha': f"{(it.get('nome') or '').strip()}: {it.get('quantidade')}",
                  'nome': (it.get('nome') or '').strip(),
                  'quantidade': int(it.get('quantidade') or 0)}
                 for it in itens_raw
                 if (it.get('nome') or '').strip() and int(it.get('quantidade') or 0) >= 0]
    resolvidos = svc.resolver_lista(parseados)
    n_ok = sum(1 for i in resolvidos if i.get('resolvido'))
    n_nao = sum(1 for i in resolvidos if not i.get('erro') and not i.get('resolvido'))
    # Pendentes tambem sao aplicados (entram como EstoqueProducao orfa) —
    # somam no delta total junto com os matched.
    delta_total = sum((i.get('delta') or 0) for i in resolvidos
                      if not i.get('erro'))
    return {
        'itens': resolvidos,
        'referencia': tool_input.get('referencia'),
        'totais': {
            'total_itens': len(resolvidos),
            'resolvidos': n_ok,
            'nao_resolvidos': n_nao,
            'delta_total': delta_total,
        },
    }


def _enriquecer_entrada_lote_loja(tool_input):
    """Enriquece itens com match + estoque atual + novo total pra preview."""
    from app.services import estoque_loja_lote as svc
    try:
        loja_id = int(tool_input.get('loja_id') or 0) or None
    except (TypeError, ValueError):
        loja_id = None
    loja_nome = None
    if loja_id:
        l = Loja.query.get(loja_id)
        if l:
            loja_nome = l.nome
        else:
            loja_id = None

    itens_raw = tool_input.get('itens') or []
    parseados = [{'linha': f"{(it.get('nome') or '').strip()}: {it.get('quantidade')}",
                  'nome': (it.get('nome') or '').strip(),
                  'quantidade': int(it.get('quantidade') or 0)}
                 for it in itens_raw
                 if (it.get('nome') or '').strip() and int(it.get('quantidade') or 0) > 0]
    resolvidos = svc.resolver_lista(parseados, loja_id) if loja_id else []
    n_ok = sum(1 for i in resolvidos if i.get('resolvido'))
    n_nao = sum(1 for i in resolvidos if not i.get('erro') and not i.get('resolvido'))
    delta_total = sum(int(i.get('quantidade') or 0) for i in resolvidos
                      if not i.get('erro'))
    return {
        'loja_id': loja_id,
        'loja_nome': loja_nome,
        'itens': resolvidos,
        'referencia': tool_input.get('referencia'),
        'totais': {
            'total_itens': len(resolvidos),
            'resolvidos': n_ok,
            'nao_resolvidos': n_nao,
            'delta_total': delta_total,
        },
    }


def _enriquecer_criar_pedido(tool_input):
    itens_enriq = []
    for item in (tool_input.get('itens') or []):
        nome = (item.get('nome') or '').strip()
        qtd = int(item.get('quantidade') or 0)
        if not nome or qtd <= 0:
            continue
        matches = _resolver_produto(nome)
        itens_enriq.append({
            'nome_original': nome, 'quantidade': qtd,
            'matches': matches, 'resolvido': matches[0] if matches else None,
        })
    loja_id = tool_input.get('loja_id')
    loja_nome = None
    if loja_id:
        l = Loja.query.get(loja_id)
        if l:
            loja_nome = l.nome
        else:
            loja_id = None
    return {
        'loja_id': loja_id, 'loja_nome': loja_nome,
        'data_entrega': tool_input.get('data_entrega'),
        'itens': itens_enriq, 'observacao': tool_input.get('observacao'),
    }


def _resolver_produto(nome):
    from sqlalchemy import func
    matches = []
    p = Produto.query.filter(func.lower(Produto.nome) == nome.lower()).first()
    if p:
        matches.append({'tipo': 'produto', 'id': p.id, 'nome': p.nome, 'match': 'exato'})
    r = Receita.query.filter(func.lower(Receita.nome) == nome.lower()).first()
    if r:
        matches.append({'tipo': 'receita', 'id': r.id, 'nome': r.nome, 'match': 'exato'})
    if matches:
        return matches
    for p in Produto.query.filter(Produto.nome.ilike(f'%{nome}%')).limit(5).all():
        matches.append({'tipo': 'produto', 'id': p.id, 'nome': p.nome, 'match': 'fuzzy'})
    for r in Receita.query.filter(Receita.nome.ilike(f'%{nome}%')).limit(5).all():
        matches.append({'tipo': 'receita', 'id': r.id, 'nome': r.nome, 'match': 'fuzzy'})
    return matches


def _resolver_mp(nome):
    from sqlalchemy import func
    matches = []
    m = MateriaPrima.query.filter(func.lower(MateriaPrima.nome) == nome.lower()).first()
    if m:
        matches.append({'id': m.id, 'nome': m.nome, 'unidade': m.unidade, 'match': 'exato'})
    if matches:
        return matches
    for m in MateriaPrima.query.filter(MateriaPrima.nome.ilike(f'%{nome}%')).limit(5).all():
        matches.append({'id': m.id, 'nome': m.nome, 'unidade': m.unidade, 'match': 'fuzzy'})
    return matches


# ── Executores READ (sem aprovacao) ───────────────────────────────────

def _executar_read(tool_name, params, user):
    try:
        if tool_name == 'consultar_pedido':
            return _read_consultar_pedido(params, user)
        if tool_name == 'consultar_estoque':
            return _read_consultar_estoque(params, user)
    except Exception as exc:  # noqa: BLE001
        logger.exception('Copilot read tool falhou')
        return {'erro': str(exc)}
    return {'erro': f'tool nao implementada: {tool_name}'}


def _read_consultar_pedido(params, user):
    from app.models import PedidoLoja
    q = PedidoLoja.query
    # Nao-admin: so consulta pedidos da propria loja
    if not user.is_admin():
        if not user.loja_id:
            return {'texto': 'Seu usuario nao tem loja vinculada.'}
        q = q.filter_by(loja_id=user.loja_id)
    if params.get('pedido_id'):
        p = q.filter_by(id=params['pedido_id']).first()
        if not p:
            return {'texto': f'Pedido #{params["pedido_id"]} nao encontrado (ou nao e da sua loja).'}
        return {'texto': _formatar_pedido(p)}
    if params.get('loja_id') and user.is_admin():
        q = q.filter_by(loja_id=params['loja_id'])
    if params.get('status'):
        q = q.filter_by(status=params['status'])
    try:
        if params.get('data_de'):
            q = q.filter(PedidoLoja.data_entrega >= datetime.strptime(params['data_de'], '%Y-%m-%d').date())
        if params.get('data_ate'):
            q = q.filter(PedidoLoja.data_entrega <= datetime.strptime(params['data_ate'], '%Y-%m-%d').date())
    except ValueError:
        pass
    pedidos = q.order_by(PedidoLoja.data_entrega.desc()).limit(15).all()
    if not pedidos:
        return {'texto': 'Nenhum pedido encontrado com esses filtros.'}
    linhas = [f'**{len(pedidos)} pedido(s) encontrado(s):**']
    for p in pedidos:
        linhas.append(f'- #{p.id} · {p.loja.nome} · {p.data_entrega.strftime("%d/%m/%Y") if p.data_entrega else "—"} · {p.status} · {len(p.itens)} itens')
    return {'texto': '\n'.join(linhas)}


def _formatar_pedido(p):
    from app.models import PedidoItem
    linhas = [f'**Pedido #{p.id}** — {p.loja.nome}']
    linhas.append(f'Data: {p.data_entrega.strftime("%d/%m/%Y") if p.data_entrega else "—"}')
    linhas.append(f'Status: {p.status}')
    if p.observacao:
        linhas.append(f'Obs: {p.observacao}')
    linhas.append('Itens:')
    for it in p.itens:
        linhas.append(f'  - {it.quantidade}× {it.nome_item}')
    return '\n'.join(linhas)


def _read_consultar_estoque(params, user):
    from app.models import MovimentacaoEstoque, AlertaEstoque
    from sqlalchemy import func as sqlfunc
    mp_nome = (params.get('mp_nome') or '').strip()
    apenas_baixo = bool(params.get('apenas_baixo'))

    if mp_nome:
        matches = _resolver_mp(mp_nome)
        if not matches:
            return {'texto': f'MP "{mp_nome}" nao encontrada.'}
        m = MateriaPrima.query.get(matches[0]['id'])
        saldo = _calcular_saldo_mp(m.id)
        alerta = AlertaEstoque.query.filter_by(materia_prima_id=m.id).first()
        txt = f'**{m.nome}**: {saldo} {m.unidade or ""} em estoque.'
        if alerta and saldo < alerta.estoque_minimo:
            txt += f'\n⚠ ABAIXO do minimo ({alerta.estoque_minimo} {m.unidade}).'
        return {'texto': txt}

    if apenas_baixo:
        alertas = AlertaEstoque.query.all()
        baixos = []
        for a in alertas:
            saldo = _calcular_saldo_mp(a.materia_prima_id)
            if saldo < a.estoque_minimo:
                m = a.materia_prima
                baixos.append(f'- {m.nome}: {saldo} {m.unidade or ""} (min: {a.estoque_minimo})')
        if not baixos:
            return {'texto': 'Nenhuma MP esta abaixo do estoque minimo.'}
        return {'texto': '**MPs em alerta:**\n' + '\n'.join(baixos)}

    return {'texto': 'Especifique uma MP ou peca pra listar as baixas (apenas_baixo=true).'}


def _calcular_saldo_mp(mp_id):
    from app.models import MovimentacaoEstoque
    from sqlalchemy import func as sqlfunc
    entradas = db.session.query(sqlfunc.coalesce(sqlfunc.sum(MovimentacaoEstoque.quantidade), 0)) \
        .filter_by(materia_prima_id=mp_id, tipo='entrada').scalar() or 0
    saidas = db.session.query(sqlfunc.coalesce(sqlfunc.sum(MovimentacaoEstoque.quantidade), 0)) \
        .filter_by(materia_prima_id=mp_id, tipo='saida').scalar() or 0
    return round(entradas - saidas, 3)


# ── Executores WRITE (aprovacao obrigatoria) ──────────────────────────

def executar(tipo_acao, params, user):
    """Roteador dos executores write. Chamado apos aprovacao no preview."""
    if tipo_acao == 'criar_pedido':
        return executar_criar_pedido(params, user)
    if tipo_acao == 'receber_mp':
        return executar_receber_mp(params, user)
    if tipo_acao == 'ajuste_estoque':
        return executar_ajuste_estoque(params, user)
    return {'ok': False, 'erro': f'tipo de acao desconhecido: {tipo_acao}'}


def executar_criar_pedido(params, user):
    from app.models import PedidoLoja, PedidoItem
    loja_id = params.get('loja_id')
    # Gerente/funcionario: forca loja_id do user, nao aceita override
    if not user.is_admin():
        if not user.loja_id:
            return {'ok': False, 'erro': 'Seu usuario nao tem loja vinculada'}
        if loja_id and loja_id != user.loja_id:
            return {'ok': False, 'erro': 'Voce so pode criar pedido pra sua loja'}
        loja_id = user.loja_id
    if not loja_id:
        return {'ok': False, 'erro': 'Loja nao especificada'}
    loja = Loja.query.get(loja_id)
    if not loja:
        return {'ok': False, 'erro': f'Loja {loja_id} nao encontrada'}
    try:
        data_entrega = datetime.strptime(params.get('data_entrega'), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return {'ok': False, 'erro': f'Data invalida'}
    itens = params.get('itens') or []
    if not itens:
        return {'ok': False, 'erro': 'Pedido sem itens'}
    pedido = PedidoLoja(
        loja_id=loja_id, data_entrega=data_entrega,
        observacao=(params.get('observacao') or '').strip() or None,
        criado_por=user.id, status='pendente',
    )
    db.session.add(pedido)
    db.session.flush()
    salvos = 0
    nao_resolvidos = []
    for item in itens:
        qtd = int(item.get('quantidade') or 0)
        if qtd <= 0:
            continue
        resolvido = item.get('resolvido')
        if not resolvido or not resolvido.get('id'):
            nao_resolvidos.append(item.get('nome_original') or '?')
            continue
        pi = PedidoItem(pedido_id=pedido.id, quantidade=qtd)
        if resolvido['tipo'] == 'produto':
            pi.produto_id = resolvido['id']
        elif resolvido['tipo'] == 'receita':
            pi.receita_id = resolvido['id']
        db.session.add(pi)
        salvos += 1
    if salvos == 0:
        db.session.rollback()
        return {'ok': False, 'erro': f'Nenhum item resolvido. Nao achei: {", ".join(nao_resolvidos)}'}
    db.session.commit()
    return {'ok': True, 'pedido_id': pedido.id, 'itens_salvos': salvos, 'nao_resolvidos': nao_resolvidos,
            'registro_tipo': 'pedido_loja', 'registro_id': pedido.id, 'url': f'/pedidos/{pedido.id}'}


def executar_receber_mp(params, user):
    from app.models import MovimentacaoEstoque
    resolvida = params.get('mp_resolvida')
    if not resolvida or not resolvida.get('id'):
        return {'ok': False, 'erro': f'MP nao identificada: {params.get("mp_nome")}'}
    quantidade = float(params.get('quantidade') or 0)
    if quantidade <= 0:
        return {'ok': False, 'erro': 'Quantidade invalida'}
    preco_unitario = params.get('preco_unitario')
    preco_total = params.get('preco_total')
    if preco_total and not preco_unitario:
        preco_unitario = float(preco_total) / quantidade
    mov = MovimentacaoEstoque(
        materia_prima_id=resolvida['id'], tipo='entrada',
        quantidade=quantidade,
        preco_unitario=float(preco_unitario) if preco_unitario else None,
        referencia=(params.get('referencia') or '').strip() or None,
        usuario_id=user.id,
    )
    db.session.add(mov)
    db.session.commit()
    return {'ok': True, 'mov_id': mov.id, 'registro_tipo': 'movimentacao_estoque', 'registro_id': mov.id}


def executar_ajuste_estoque(params, user):
    from app.models import MovimentacaoEstoque
    resolvida = params.get('mp_resolvida')
    if not resolvida or not resolvida.get('id'):
        return {'ok': False, 'erro': f'MP nao identificada: {params.get("mp_nome")}'}
    quantidade = float(params.get('quantidade') or 0)
    tipo = params.get('tipo')
    motivo = (params.get('motivo') or '').strip()
    if quantidade <= 0 or tipo not in ('entrada', 'saida') or not motivo:
        return {'ok': False, 'erro': 'Parametros invalidos'}
    mov = MovimentacaoEstoque(
        materia_prima_id=resolvida['id'], tipo=tipo,
        quantidade=quantidade,
        referencia=f'Ajuste: {motivo}',
        usuario_id=user.id,
    )
    db.session.add(mov)
    db.session.commit()
    return {'ok': True, 'mov_id': mov.id, 'registro_tipo': 'movimentacao_estoque', 'registro_id': mov.id}


# ───── Tools READ novas ────────────────────────────────────────────────

def _read_consultar_fornecedores(params, user):
    from app.models import Fornecedor
    q = Fornecedor.query
    if params.get('apenas_ativos', True):
        q = q.filter_by(ativo=True)
    busca = (params.get('busca') or '').strip()
    if busca:
        q = q.filter(Fornecedor.nome.ilike(f'%{busca}%'))
    forns = q.order_by(Fornecedor.nome).limit(30).all()
    if not forns:
        return {'texto': 'Nenhum fornecedor encontrado.'}
    linhas = [f'**{len(forns)} fornecedor(es):**']
    for f in forns:
        linhas.append(f'- {f.nome}' + (f' (contato: {f.contato})' if f.contato else ''))
    return {'texto': '\n'.join(linhas)}


def _read_consultar_margem(params, user):
    from app.services.custos import calcular_custos_receitas, calcular_rendimento
    nome = (params.get('nome') or '').strip()
    if not nome:
        return {'texto': 'Informe o nome.'}
    resultado = calcular_custos_receitas()
    custos = resultado.get('custos', {})
    r = Receita.query.filter(Receita.nome.ilike(nome)).first()
    if not r:
        # Tenta produto
        p = Produto.query.filter(Produto.nome.ilike(nome)).first()
        if not p:
            return {'texto': f'"{nome}" nao encontrado.'}
        return {'texto': f'**{p.nome}** (produto): atacado R$ {p.preco_atacado or 0:.2f}, loja R$ {p.preco_loja or 0:.2f}, site R$ {p.preco_site or 0:.2f}.'}
    custo_un = custos.get(r.nome, 0)
    rendimento = calcular_rendimento(r)
    def margem(p): return ((p - custo_un) / p * 100) if p and p > 0 else None
    linhas = [f'**{r.nome}** (receita)']
    linhas.append(f'- Custo unitário: R$ {custo_un:.4f}')
    linhas.append(f'- Rendimento: {rendimento}')
    for label, preco in [('Atacado', r.preco_venda), ('Loja', r.preco_loja), ('Site', r.preco_site)]:
        if preco:
            m = margem(preco)
            linhas.append(f'- {label}: R$ {preco:.2f} (margem {m:.1f}%)' if m else f'- {label}: R$ {preco:.2f}')
    return {'texto': '\n'.join(linhas)}


def _read_consultar_funcionario(params, user):
    from app.models import Funcionario
    nome = (params.get('nome') or '').strip()
    if not nome:
        return {'texto': 'Informe o nome.'}
    f = Funcionario.query.filter(Funcionario.nome.ilike(f'%{nome}%')).first()
    if not f:
        return {'texto': f'"{nome}" nao encontrado.'}
    lojas = ', '.join(l.nome for l in f.lojas) or '—'
    status = 'ativo' if f.ativo else 'inativo'
    linhas = [f'**{f.nome}** ({status})']
    linhas.append(f'- Função: {f.funcao or f.funcao_operacional or "—"}')
    linhas.append(f'- Período: {f.periodo or "—"}')
    linhas.append(f'- Lojas: {lojas}')
    # Salario so pra super admin (is_owner)
    if getattr(user, 'is_owner', False):
        linhas.append(f'- Salário base: R$ {(f.salario_base or 0):.2f}')
    if f.telefone:
        linhas.append(f'- Telefone: {f.telefone}')
    if f.data_admissao:
        linhas.append(f'- Admitido em: {f.data_admissao.strftime("%d/%m/%Y")}')
    return {'texto': '\n'.join(linhas)}


def _read_consultar_caixa(params, user):
    from datetime import date as _date
    from app.models import PedidoLocal, AtribuicaoEntrega, MovimentacaoEstoque
    from sqlalchemy import func as sqlfunc
    data_str = params.get('data')
    try:
        d = datetime.strptime(data_str, '%Y-%m-%d').date() if data_str else _date.today()
    except ValueError:
        d = _date.today()
    locais = PedidoLocal.query.filter(PedidoLocal.data_entrega == d).all()
    valor_locais = sum(p.valor_total for p in locais)
    atribs = AtribuicaoEntrega.query.filter(AtribuicaoEntrega.data_entrega == d).all()
    movs = MovimentacaoEstoque.query.filter(
        MovimentacaoEstoque.tipo == 'entrada',
        sqlfunc.date(MovimentacaoEstoque.data) == d,
    ).all()
    valor_compras = sum((m.quantidade or 0) * (m.preco_unitario or 0) for m in movs)
    linhas = [f'**Resumo de {d.strftime("%d/%m/%Y")}:**']
    linhas.append(f'- {len(locais)} pedido(s) local → R$ {valor_locais:.2f}')
    feitas = sum(1 for a in atribs if a.status == 'entregue')
    falhas = sum(1 for a in atribs if a.status == 'nao_entregue')
    linhas.append(f'- {len(atribs)} entregas atribuidas ({feitas} feitas, {falhas} falhas)')
    linhas.append(f'- {len(movs)} compras de MP → R$ {valor_compras:.2f}')
    return {'texto': '\n'.join(linhas)}


def _read_consultar_vendas_itens(params, user):
    """Agrega itens vendidos da Seru no intervalo (e loja opcional)."""
    from datetime import date as _date
    from app.services import vendas_itens
    hoje = _date.today()
    try:
        ini = datetime.strptime(params['inicio'], '%Y-%m-%d').date() if params.get('inicio') else hoje
    except (ValueError, TypeError):
        ini = hoje
    try:
        fim = datetime.strptime(params['fim'], '%Y-%m-%d').date() if params.get('fim') else hoje
    except (ValueError, TypeError):
        fim = hoje
    if fim < ini:
        ini, fim = fim, ini
    if (fim - ini).days > 92:
        return {'texto': 'Intervalo máximo é 92 dias.'}
    loja = (params.get('loja') or '').strip() or None
    top = max(1, min(int(params.get('top') or 10), 30))

    dias_ate_hoje = max(0, (hoje - fim).days) if fim < hoje else 0
    dias_extra = min(dias_ate_hoje, 7)
    try:
        data = vendas_itens.agregar_itens(ini, fim, loja_seru=loja,
                                          expandir_dias_frente=dias_extra)
    except Exception as e:
        logger.exception('consultar_vendas_itens falhou')
        return {'erro': f'{type(e).__name__}: {str(e)[:300]}'}

    if not data['produtos']:
        sufixo = f' na loja "{loja}"' if loja else ''
        return {'texto': f'Nenhuma venda encontrada de {ini.strftime("%d/%m")} a {fim.strftime("%d/%m")}{sufixo}.'}

    cab = f'**Vendas {ini.strftime("%d/%m")} → {fim.strftime("%d/%m")}**'
    if loja:
        cab += f' · {loja}'
    cab += f' · {data["total_pedidos"]} pedido(s) · R$ {data["faturamento_total"]:.2f}'

    linhas = [cab, '']
    for i, p in enumerate(data['produtos'][:top], 1):
        match_str = ''
        if p['match']:
            kind = ' (fuzzy)' if p['match']['kind'] == 'fuzzy' else ''
            match_str = f' ↔ {p["match"]["nome"]}{kind}'
        else:
            match_str = ' ⚠ sem match no sistema'
        linhas.append(
            f'{i}. **{p["nome"]}** — {int(p["qtd"])} un · R$ {p["faturamento"]:.2f} ({p["pct_faturamento"]:.0f}%){match_str}'
        )
    if data['sem_match_count']:
        linhas.append('')
        linhas.append(f'_{data["sem_match_count"]} produto(s) Seru sem match no cadastro._')
    if not loja and data['lojas_no_intervalo']:
        linhas.append('')
        linhas.append('Lojas no intervalo: ' + ', '.join(data['lojas_no_intervalo']))
    return {'texto': '\n'.join(linhas)}


# Roteador read estendido
def _executar_read(tool_name, params, user):  # noqa: F811
    try:
        if tool_name == 'consultar_pedido':
            return _read_consultar_pedido(params, user)
        if tool_name == 'consultar_estoque':
            return _read_consultar_estoque(params, user)
        if tool_name == 'consultar_fornecedores':
            return _read_consultar_fornecedores(params, user)
        if tool_name == 'consultar_margem':
            return _read_consultar_margem(params, user)
        if tool_name == 'consultar_funcionario':
            return _read_consultar_funcionario(params, user)
        if tool_name == 'consultar_caixa':
            return _read_consultar_caixa(params, user)
        if tool_name == 'consultar_vendas_itens':
            return _read_consultar_vendas_itens(params, user)
    except Exception as exc:  # noqa: BLE001
        logger.exception('Copilot read tool falhou')
        return {'erro': str(exc)}
    return {'erro': f'tool nao implementada: {tool_name}'}


# ───── Tools WRITE novas ───────────────────────────────────────────────

def executar_mudar_status_pedido(params, user):
    from app.models import PedidoLoja
    pid = params.get('pedido_id')
    novo = params.get('novo_status')
    p = PedidoLoja.query.get(pid)
    if not p:
        return {'ok': False, 'erro': f'Pedido #{pid} nao encontrado'}
    # Restricao de loja pra nao-admin
    if not user.is_admin() and p.loja_id != user.loja_id:
        return {'ok': False, 'erro': f'Pedido #{pid} nao e da sua loja'}
    # Transicoes validas
    transicoes = {
        'confirmar': ('pendente', 'confirmado'),
        'separar': (('pendente', 'confirmado'), 'separado'),
        'enviar': ('separado', 'em_transporte'),
        'cancelar': (('pendente', 'confirmado', 'separado', 'em_transporte'), 'cancelado'),
    }
    if novo not in transicoes:
        return {'ok': False, 'erro': f'status invalido: {novo}'}
    de, para = transicoes[novo]
    if isinstance(de, str):
        de = (de,)
    if p.status not in de:
        return {'ok': False, 'erro': f'Pedido #{pid} esta {p.status}, nao pode {novo}'}
    p.status = para
    db.session.commit()
    return {'ok': True, 'pedido_id': pid, 'novo_status': para,
            'registro_tipo': 'pedido_loja', 'registro_id': pid,
            'url': f'/pedidos/{pid}'}


def executar_criar_fornecedor(params, user):
    from app.models import Fornecedor
    nome = (params.get('nome') or '').strip()
    if not nome:
        return {'ok': False, 'erro': 'Nome obrigatorio'}
    if Fornecedor.query.filter_by(nome=nome).first():
        return {'ok': False, 'erro': f'Ja existe fornecedor "{nome}"'}
    f = Fornecedor(
        nome=nome,
        cnpj=(params.get('cnpj') or '').strip() or None,
        telefone=(params.get('telefone') or '').strip() or None,
        email=(params.get('email') or '').strip() or None,
        contato=(params.get('contato') or '').strip() or None,
        observacao=(params.get('observacao') or '').strip() or None,
    )
    db.session.add(f)
    db.session.commit()
    return {'ok': True, 'fornecedor_id': f.id, 'nome': f.nome,
            'registro_tipo': 'fornecedor', 'registro_id': f.id,
            'url': f'/fornecedores/{f.id}'}


def executar_marcar_ponto(params, user):
    from app.models import Funcionario, RegistroPonto
    nome = (params.get('funcionario_nome') or '').strip()
    tipo = params.get('tipo')
    if not nome or not tipo:
        return {'ok': False, 'erro': 'Faltam parametros'}
    f = Funcionario.query.filter(Funcionario.nome.ilike(f'%{nome}%')).first()
    if not f:
        return {'ok': False, 'erro': f'Funcionario "{nome}" nao encontrado'}
    # RegistroPonto pode ter outro nome — tenta varias possibilidades
    try:
        from app.models import RegistroPonto as _RP
    except ImportError:
        return {'ok': False, 'erro': 'Modelo RegistroPonto nao existe — funcionalidade pendente'}
    rp = _RP(
        funcionario_id=f.id,
        tipo=tipo,
        timestamp=agora(),
    )
    db.session.add(rp)
    db.session.commit()
    return {'ok': True, 'funcionario': f.nome, 'tipo': tipo,
            'registro_tipo': 'registro_ponto', 'registro_id': rp.id}


def executar_criar_tarefa(params, user):
    """Cria tarefa em projetos. Default: Inbox."""
    titulo = (params.get('titulo') or '').strip()
    if not titulo:
        return {'ok': False, 'erro': 'Titulo obrigatorio'}
    try:
        from app.models import TarefaProjeto, Projeto
    except ImportError:
        return {'ok': False, 'erro': 'Modelo TarefaProjeto/Projeto nao existe'}

    projeto = None
    proj_nome = (params.get('projeto_nome') or '').strip()
    if proj_nome:
        projeto = Projeto.query.filter(Projeto.nome.ilike(f'%{proj_nome}%')).first()

    prazo = None
    if params.get('data_prazo'):
        try:
            prazo = datetime.strptime(params['data_prazo'], '%Y-%m-%d').date()
        except ValueError:
            pass

    t = TarefaProjeto(
        nome=titulo,
        projeto_id=projeto.id if projeto else None,
        data_prazo=prazo,
        criado_por=user.id,
    )
    db.session.add(t)
    db.session.commit()
    return {'ok': True, 'tarefa_id': t.id, 'titulo': titulo,
            'projeto': projeto.nome if projeto else 'Inbox',
            'registro_tipo': 'tarefa_projeto', 'registro_id': t.id}


# Roteador write estendido
def executar(tipo_acao, params, user):  # noqa: F811
    if tipo_acao == 'criar_pedido':
        return executar_criar_pedido(params, user)
    if tipo_acao == 'receber_mp':
        return executar_receber_mp(params, user)
    if tipo_acao == 'ajuste_estoque':
        return executar_ajuste_estoque(params, user)
    if tipo_acao == 'mudar_status_pedido':
        return executar_mudar_status_pedido(params, user)
    if tipo_acao == 'criar_fornecedor':
        return executar_criar_fornecedor(params, user)
    if tipo_acao == 'marcar_ponto':
        return executar_marcar_ponto(params, user)
    if tipo_acao == 'criar_tarefa':
        return executar_criar_tarefa(params, user)
    return {'ok': False, 'erro': f'tipo de acao desconhecido: {tipo_acao}'}


# ───── Tools de Planejamento — READ ────────────────────────────────────

def _read_consultar_foco(params, user):
    """Lista projetos foco_12s + tarefas pendentes deles."""
    from app.models import Projeto, TarefaProjeto
    projetos = (Projeto.query.filter_by(foco_12s=True)
                .order_by(Projeto.prioridade.desc(), Projeto.nome).all())
    if not projetos:
        return {'texto': 'Nenhum projeto marcado como foco das 12 semanas. Marque em /projetos.'}
    linhas = [f'**{len(projetos)} projeto(s) em FOCO:**']
    for p in projetos:
        tarefas = [t for t in p.tarefas if t.status not in ('feito', 'cancelado')]
        atrasadas = sum(1 for t in tarefas if t.atrasada)
        info = f'{len(tarefas)} tarefa(s) pendente(s)'
        if atrasadas:
            info += f' · ⚠ {atrasadas} atrasada(s)'
        linhas.append(f'\n**[{p.id}] {p.nome}** — {info}')
        for t in tarefas[:5]:
            prazo = f' (prazo {t.prazo.strftime("%d/%m")})' if t.prazo else ''
            atrasada = ' ⚠' if t.atrasada else ''
            linhas.append(f'  - #{t.id} {t.nome}{prazo}{atrasada}')
        if len(tarefas) > 5:
            linhas.append(f'  ... +{len(tarefas) - 5} tarefas')
    return {'texto': '\n'.join(linhas)}


def _read_consultar_tarefas(params, user):
    from app.models import Projeto, TarefaProjeto
    from sqlalchemy import and_
    q = TarefaProjeto.query.join(Projeto)
    if params.get('apenas_pendentes', True):
        q = q.filter(TarefaProjeto.status.notin_(['feito', 'cancelado']))
    if params.get('apenas_atrasadas'):
        q = q.filter(TarefaProjeto.prazo.isnot(None), TarefaProjeto.prazo < date.today())
    if params.get('apenas_foco'):
        q = q.filter(Projeto.foco_12s == True)
    proj_nome = (params.get('projeto_nome') or '').strip()
    if proj_nome:
        q = q.filter(Projeto.nome.ilike(f'%{proj_nome}%'))
    tarefas = q.order_by(TarefaProjeto.prazo.asc().nulls_last(), TarefaProjeto.id).limit(40).all()
    if not tarefas:
        return {'texto': 'Nenhuma tarefa encontrada com esses filtros.'}
    linhas = [f'**{len(tarefas)} tarefa(s):**']
    for t in tarefas:
        prazo = f' · prazo {t.prazo.strftime("%d/%m/%Y")}' if t.prazo else ''
        atrasada = ' ⚠ ATRASADA' if t.atrasada else ''
        foco = ' 🎯' if t.projeto.foco_12s else ''
        linhas.append(f'- #{t.id} [{t.projeto.nome}{foco}] {t.nome}{prazo}{atrasada}')
    return {'texto': '\n'.join(linhas)}


# ───── Tools de Planejamento — WRITE ───────────────────────────────────

def executar_marcar_tarefa_feita(params, user):
    from app.models import TarefaProjeto
    tid = params.get('tarefa_id')
    t = TarefaProjeto.query.get(tid)
    if not t:
        return {'ok': False, 'erro': f'Tarefa #{tid} nao encontrada'}
    if t.status == 'feito':
        return {'ok': False, 'erro': f'Tarefa #{tid} ja esta marcada como feita'}
    t.status = 'feito'
    t.feito_em = agora()
    db.session.commit()
    return {'ok': True, 'tarefa_id': tid, 'nome': t.nome,
            'registro_tipo': 'tarefa_projeto', 'registro_id': tid}


def executar_balanco_congelados(params, user):
    """Aplica balanco/inventario de congelados. Re-resolve nomes se necessario."""
    from app.services import estoque_congelados as svc
    itens = params.get('itens') or []
    if not itens:
        return {'ok': False, 'erro': 'Lista de itens vazia'}
    # Garante que cada item tem 'resolvido' — se nao tiver, resolve agora
    precisa_resolver = any(not (i.get('erro') or i.get('resolvido')) for i in itens)
    if precisa_resolver:
        parseados = [{'linha': f"{(i.get('nome') or '').strip()}: {i.get('quantidade')}",
                      'nome': (i.get('nome') or '').strip(),
                      'quantidade': int(i.get('quantidade') or 0)}
                     for i in itens
                     if (i.get('nome') or '').strip() and int(i.get('quantidade') or 0) >= 0]
        itens = svc.resolver_lista(parseados)

    referencia = (params.get('referencia') or '').strip() or None
    resultado = svc.aplicar_balanco(itens, user, referencia=referencia)
    n_ok = len(resultado['aplicados'])
    n_ign = len(resultado['ignorados'])
    if n_ok == 0:
        return {'ok': False, 'erro': f'Nenhum item aplicado. {n_ign} ignorados.',
                'ignorados': resultado['ignorados']}
    return {'ok': True, 'aplicados': resultado['aplicados'],
            'ignorados': resultado['ignorados'],
            'total_aplicados': n_ok, 'total_ignorados': n_ign,
            'registro_tipo': 'estoque_producao_balanco',
            'registro_id': None,
            'url': '/pedidos/congelados'}


# Re-defino os roteadores pra incluir as novas tools.
# Como _executar_read e executar foram redefinidos com `# noqa: F811`,
# preciso adicionar mais um nivel.

_BASE_READ = _executar_read
_BASE_EXEC = executar


def _executar_read(tool_name, params, user):  # noqa: F811
    try:
        if tool_name == 'consultar_foco':
            return _read_consultar_foco(params, user)
        if tool_name == 'consultar_tarefas':
            return _read_consultar_tarefas(params, user)
    except Exception as exc:  # noqa: BLE001
        logger.exception('Copilot read tool falhou')
        return {'erro': str(exc)}
    return _BASE_READ(tool_name, params, user)


def executar_entrada_lote_loja(params, user):
    """Aplica entrada em lote no estoque de uma loja. Resolve nomes se preciso."""
    from app.services import estoque_loja_lote as svc
    try:
        loja_id = int(params.get('loja_id') or 0)
    except (TypeError, ValueError):
        loja_id = 0
    if not loja_id:
        return {'ok': False, 'erro': 'Loja nao especificada'}

    itens = params.get('itens') or []
    if not itens:
        return {'ok': False, 'erro': 'Lista de itens vazia'}

    precisa_resolver = any(not (i.get('erro') or i.get('resolvido')) for i in itens)
    if precisa_resolver:
        parseados = [{'linha': f"{(i.get('nome') or '').strip()}: {i.get('quantidade')}",
                      'nome': (i.get('nome') or '').strip(),
                      'quantidade': int(i.get('quantidade') or 0)}
                     for i in itens
                     if (i.get('nome') or '').strip() and int(i.get('quantidade') or 0) > 0]
        itens = svc.resolver_lista(parseados, loja_id)

    referencia = (params.get('referencia') or '').strip() or None
    resultado = svc.aplicar_entrada_lote(itens, loja_id, user, referencia=referencia)
    n_ok = len(resultado['aplicados'])
    n_ign = len(resultado['ignorados'])
    if n_ok == 0:
        return {'ok': False, 'erro': f'Nenhum item aplicado. {n_ign} ignorados.',
                'ignorados': resultado['ignorados']}
    return {'ok': True, 'aplicados': resultado['aplicados'],
            'ignorados': resultado['ignorados'],
            'total_aplicados': n_ok, 'total_ignorados': n_ign,
            'registro_tipo': 'estoque_loja_entrada_lote',
            'registro_id': None,
            'url': f'/pedidos/estoque-loja?loja={loja_id}'}


def executar(tipo_acao, params, user):  # noqa: F811
    if tipo_acao == 'marcar_tarefa_feita':
        return executar_marcar_tarefa_feita(params, user)
    if tipo_acao == 'balanco_congelados':
        return executar_balanco_congelados(params, user)
    if tipo_acao == 'entrada_lote_loja':
        return executar_entrada_lote_loja(params, user)
    if tipo_acao == 'registrar_desperdicio':
        return executar_registrar_desperdicio(params, user)
    return _BASE_EXEC(tipo_acao, params, user)


# ── Desperdicio (sobra do dia / vencido) ───────────────────────────────

def _resolver_item_qualquer(nome):
    """Match flex: tenta Receita, Produto e MP. Retorna (tipo, id, nome) ou None.

    Tipo retornado: 'receita' | 'produto' | 'mp'.
    """
    from sqlalchemy import func
    nome = (nome or '').strip()
    if not nome:
        return None
    r = Receita.query.filter(func.lower(Receita.nome) == nome.lower()).first()
    if r:
        return ('receita', r.id, r.nome)
    p = Produto.query.filter(func.lower(Produto.nome) == nome.lower()).first()
    if p:
        return ('produto', p.id, p.nome)
    m = MateriaPrima.query.filter(func.lower(MateriaPrima.nome) == nome.lower()).first()
    if m:
        return ('mp', m.id, m.nome)
    r = Receita.query.filter(Receita.nome.ilike(f'%{nome}%')).first()
    if r:
        return ('receita', r.id, r.nome)
    p = Produto.query.filter(Produto.nome.ilike(f'%{nome}%')).first()
    if p:
        return ('produto', p.id, p.nome)
    m = MateriaPrima.query.filter(MateriaPrima.nome.ilike(f'%{nome}%')).first()
    if m:
        return ('mp', m.id, m.nome)
    return None


def _resolver_loja_para_user(loja_id, loja_nome, user):
    """Resolve loja: admin pode passar id/nome; gerente forca user.loja_id."""
    if not user.is_admin():
        if user.loja_id:
            l = Loja.query.get(user.loja_id)
            if l:
                return l
        return None
    if loja_id:
        return Loja.query.get(int(loja_id))
    if loja_nome:
        from sqlalchemy import func
        l = Loja.query.filter(func.lower(Loja.nome) == loja_nome.lower()).first()
        if l:
            return l
        return Loja.query.filter(Loja.nome.ilike(f'%{loja_nome}%')).first()
    return None


def executar_registrar_desperdicio(params, user):
    from app.models import Desperdicio, EstoqueLoja, MovEstoqueLoja
    loja = _resolver_loja_para_user(params.get('loja_id'),
                                     params.get('loja_nome'), user)
    if not loja:
        return {'ok': False, 'erro': 'Loja nao identificada. Informe loja_nome.'}

    nome_item = (params.get('item_nome') or '').strip()
    if not nome_item:
        return {'ok': False, 'erro': 'item_nome obrigatorio.'}
    resolvido = _resolver_item_qualquer(nome_item)
    if not resolvido:
        return {'ok': False, 'erro': f'Item nao encontrado no cadastro: "{nome_item}"'}
    tipo_item, item_id, nome_item_ok = resolvido

    try:
        qtd = int(params.get('quantidade') or 0)
    except (TypeError, ValueError):
        qtd = 0
    if qtd <= 0:
        return {'ok': False, 'erro': 'Quantidade deve ser > 0.'}

    motivo = (params.get('motivo') or 'vencido').strip() or 'vencido'
    if motivo not in ('vencido', 'estragado', 'queimado', 'caiu', 'outro'):
        motivo = 'vencido'
    observacao = (params.get('observacao') or '').strip() or None

    filtro = {'loja_id': loja.id}
    if tipo_item == 'receita':
        filtro['receita_id'] = item_id
    elif tipo_item == 'produto':
        filtro['produto_id'] = item_id
    else:
        filtro['materia_prima_id'] = item_id

    el = EstoqueLoja.query.filter_by(**filtro).first()
    if not el:
        el = EstoqueLoja(**filtro, quantidade=0)
        db.session.add(el)
        db.session.flush()

    saldo = el.quantidade or 0
    baixa = min(qtd, saldo)
    el.quantidade = saldo - baixa

    desp = Desperdicio(
        loja_id=loja.id,
        receita_id=item_id if tipo_item == 'receita' else None,
        produto_id=item_id if tipo_item == 'produto' else None,
        materia_prima_id=item_id if tipo_item == 'mp' else None,
        quantidade=qtd, motivo=motivo, observacao=observacao,
        criado_por_id=user.id,
    )
    db.session.add(desp)

    if baixa > 0:
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo='desperdicio', quantidade=baixa,
            referencia=f'Desperdicio {motivo}'
            + (f' — {observacao}' if observacao else '')
            + ' (copilot)',
            usuario_id=user.id,
        ))
    if qtd > baixa:
        falta = qtd - baixa
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo='desperdicio_sem_estoque',
            quantidade=falta,
            referencia=f'Desperdicio {motivo} — registrado sem estoque ({falta}) (copilot)',
            usuario_id=user.id,
        ))

    db.session.commit()
    return {'ok': True, 'desperdicio_id': desp.id,
            'loja': loja.nome, 'item': nome_item_ok,
            'quantidade': qtd, 'baixado_do_estoque': baixa,
            'motivo': motivo,
            'registro_tipo': 'desperdicio', 'registro_id': desp.id,
            'url': f'/pedidos/desperdicio?loja={loja.id}'}


def _read_consultar_desperdicio(params, user):
    from app.models import Desperdicio
    try:
        dias = int(params.get('dias') or 7)
    except (TypeError, ValueError):
        dias = 7
    dias = max(1, min(90, dias))
    desde = hoje() - timedelta(days=dias)

    q = Desperdicio.query.filter(Desperdicio.data >= desde)

    loja_nome = (params.get('loja_nome') or '').strip()
    if loja_nome:
        from sqlalchemy import func
        l = Loja.query.filter(
            (func.lower(Loja.nome) == loja_nome.lower())
            | (Loja.nome.ilike(f'%{loja_nome}%'))
        ).first()
        if not l:
            return {'texto': f'Loja "{loja_nome}" nao encontrada.'}
        q = q.filter(Desperdicio.loja_id == l.id)
    elif not user.is_admin() and user.loja_id:
        q = q.filter(Desperdicio.loja_id == user.loja_id)

    registros = q.order_by(Desperdicio.data.desc(), Desperdicio.criado_em.desc()).all()
    if not registros:
        return {'texto': f'Nenhum desperdicio nos ultimos {dias} dias.'}

    total_qtd = sum(d.quantidade for d in registros)
    linhas = [f'**Desperdicio — ultimos {dias} dias ({len(registros)} reg, {total_qtd} un):**']

    por_item = {}
    for d in registros:
        key = d.nome_item
        por_item.setdefault(key, 0)
        por_item[key] += d.quantidade
    top = sorted(por_item.items(), key=lambda x: -x[1])[:10]
    linhas.append('')
    linhas.append('**Por item (top 10):**')
    for nome, qt in top:
        linhas.append(f'- {nome}: {qt} un')

    linhas.append('')
    linhas.append('**Registros recentes:**')
    for d in registros[:15]:
        l_nome = d.loja.nome if d.loja else '?'
        linhas.append(f'- {d.data.strftime("%d/%m")} · {l_nome} · {d.nome_item}: '
                      f'{d.quantidade} un ({d.motivo})')

    return {'texto': '\n'.join(linhas)}


# Roteador read final — inclui consultar_desperdicio
_BASE_READ2 = _executar_read


def _executar_read(tool_name, params, user):  # noqa: F811
    if tool_name == 'consultar_desperdicio':
        try:
            return _read_consultar_desperdicio(params, user)
        except Exception as exc:  # noqa: BLE001
            logger.exception('consultar_desperdicio falhou')
            return {'erro': str(exc)}
    return _BASE_READ2(tool_name, params, user)

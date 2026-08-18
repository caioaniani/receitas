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
import logging
import os
import secrets
from datetime import date, datetime, timedelta

from flask import current_app

from app.extensions import db
from app.models import Loja, MateriaPrima, Produto, Receita
from app.utils import agora, hoje

logger = logging.getLogger(__name__)

# Modelo default do copilot (Slack e canais operacionais que NAO passam
# override `modelo=`).
#
# Historico do fork (14/06/2026, decisao do dono):
# - WhatsApp do dono (zapi_bot.py) passa `modelo=MODELO_WHATSAPP_DEFAULT`
#   = Opus 4.8 — caminho premium pra ele.
# - Slack (slack_bot.py) NAO passa override — opera em Sonnet 4.6,
#   modelo default mais barato. Decidido apos a janela de tentar Opus
#   default em todos os canais: o ganho de qualidade no Slack nao
#   compensou o custo extra com 12 atendentes usando.
MODELO_DEFAULT = 'claude-sonnet-5'


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
            "loja_id": {"type": ["integer", "null"], "description": "ID da loja. Null se nao souber o id."},
            "loja_nome": {"type": ["string", "null"], "description": "Nome da loja (ex: 'Ribeiro do Vale'). SEMPRE passe um dos dois (loja_id OU loja_nome) — o servidor faz fuzzy match. Se o usuario nao mencionou a loja, NAO chame a tool: pergunte primeiro qual loja."},
            "data_entrega": {"type": "string", "description": "Data YYYY-MM-DD. Resolva 'amanha'/'sexta'/etc."},
            "itens": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "nome": {"type": "string", "description": "Nome EXATO do item do catalogo — pode ser produto, receita OU materia-prima (loja pede MPs tambem: queijo, lagarto cozido, saco de pao de queijo, etc)."},
                        "quantidade": {"type": "integer", "minimum": 1},
                        "estado": {"type": ["string", "null"], "enum": [None, "backup", "assado"], "description": "Estado do item: null = padrao da familia (cru congelado pra viennoiserie / congelado assado pra pao); 'backup' = pre-fermentado congelado (assa rapido pra repor vitrine); 'assado' = ja assado (raro, so se a loja pediu explicitamente). Pedido misto (ex: '5 croissants + 3 backup') vira 2 linhas — uma com estado=null, outra com estado='backup'. NUNCA consolide quantidades de estados distintos. Gatilhos pra `backup`: 'backup', 'fermentado e congelado', 'pre-fermentado', 'pre-fermentados congelados', 'fermentados congelados'. Gatilhos pra `assado`: 'assado(s)', 'ja assado'. Gatilhos pra `null`: 'congelado' sozinho, 'cru', sem qualificador."},
                        "observacao": {"type": ["string", "null"], "description": "Observacao livre do item (max 200 chars): 'sem cebola', 'recheio extra'. NAO use pra estado — use o campo `estado` acima. Default null."},
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

TOOL_EDITAR_PEDIDO = {
    "name": "editar_pedido",
    "description": (
        "Edita um pedido existente. APENAS status 'pendente' ou 'confirmado' — "
        "depois disso (separado/em_transporte/entregue) o estoque ja foi tocado "
        "e a edicao e bloqueada. NAO muda loja nem driver (pra isso cancele e recrie). "
        "Se for mexer em itens, mande a LISTA COMPLETA — REPLACE total. Use "
        "consultar_pedido antes pra saber a composicao atual."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pedido_id": {"type": "integer", "description": "ID do pedido a editar."},
            "data_entrega": {"type": ["string", "null"], "description": "Nova data YYYY-MM-DD, ou null pra manter a atual."},
            "observacao": {"type": ["string", "null"], "description": "Nova observacao do pedido. String vazia limpa; null mantem."},
            "itens": {
                "type": ["array", "null"],
                "description": ("Se enviado, SUBSTITUI todos os itens do pedido. Pra alterar 1 item, "
                                 "envie TODOS (os atuais + as mudancas). Se omitido ou null, mantem os atuais. "
                                 "Mesmo schema dos itens de criar_pedido."),
                "items": {
                    "type": "object",
                    "properties": {
                        "nome": {"type": "string", "description": "Nome EXATO do item do catalogo — pode ser produto, receita OU materia-prima (loja pede MPs tambem: queijo, lagarto cozido, saco de pao de queijo, etc)."},
                        "quantidade": {"type": "integer", "minimum": 1},
                        "estado": {"type": ["string", "null"], "enum": [None, "backup", "assado"], "description": "Mesmos gatilhos de criar_pedido: 'backup'/'fermentado e congelado'/'pre-fermentado' = backup; 'assado(s)' = assado; 'congelado'/'cru'/sem qualificador = null."},
                        "observacao": {"type": ["string", "null"]},
                    },
                    "required": ["nome", "quantidade"],
                },
            },
        },
        "required": ["pedido_id"],
    },
}


TOOL_CONSULTAR_PEDIDO = {
    "name": "consultar_pedido",
    "description": ("Consulta pedidos por loja, data, status ou ID. "
                    "formato='lista' (default): linhas resumidas com #ID/loja/data/status. "
                    "formato='detalhe': cada pedido com seus itens listados. "
                    "formato='agregado': resumo igual ao do canal #producao das 04h — "
                    "total do dia somado por item + breakdown por loja. "
                    "Use 'agregado' quando o usuario perguntar 'quanto a industria precisa "
                    "produzir', 'resumo de producao do dia', 'pedidos consolidados'. "
                    "Use 'detalhe' quando ele pedir 'pode detalhar', 'mostra os itens', "
                    "'o que tem em cada pedido'. "
                    "Por padrao EXCLUI pedidos entregues e cancelados — passe "
                    "incluir_finalizados=true se o usuario pediu pra ver TODOS, "
                    "ou passe status='entregue'/'cancelado' explicitamente pra ver so esses."),
    "input_schema": {
        "type": "object",
        "properties": {
            "loja_id": {"type": ["integer", "null"]},
            "data_de": {"type": ["string", "null"], "description": "Data inicial YYYY-MM-DD."},
            "data_ate": {"type": ["string", "null"], "description": "Data final YYYY-MM-DD."},
            "status": {"type": ["string", "null"], "enum": [None, "pendente", "confirmado", "separado", "em_transporte", "entregue", "cancelado"]},
            "pedido_id": {"type": ["integer", "null"]},
            "incluir_finalizados": {"type": ["boolean", "null"],
                "description": "Default false. Se true, inclui pedidos entregue + cancelado. Use SO quando o usuario pedir explicitamente pra ver tudo."},
            "formato": {"type": ["string", "null"], "enum": [None, "lista", "detalhe", "agregado"],
                "description": "Default 'lista'. 'detalhe' = itens de cada pedido. 'agregado' = total do dia + por loja (estilo resumo 04h)."},
        },
    },
}

TOOL_CONSULTAR_ESTOQUE = {
    "name": "consultar_estoque",
    "description": ("Consulta estoque. Escopo determina onde olhar: "
                    "'mp' (materias-primas), 'producao' (freezer/produtos prontos da industria), "
                    "'loja' (uma loja especifica), 'todos' (mp + producao + todas as lojas — "
                    "use quando o usuario pedir visao geral ou 'em todas as lojas'). "
                    "Sempre informe escopo. Pra escopo='loja' COM loja especifica, informe loja_nome; "
                    "sem loja_nome, lista todas. Use item_nome pra filtrar um produto especifico."),
    "input_schema": {
        "type": "object",
        "properties": {
            "escopo": {"type": "string", "enum": ["mp", "producao", "loja", "todos"],
                       "description": "Onde olhar."},
            "item_nome": {"type": ["string", "null"],
                          "description": "Nome do item (filtra por substring)."},
            "loja_nome": {"type": ["string", "null"],
                          "description": "Nome da loja (so com escopo='loja')."},
            "apenas_baixo": {"type": "boolean",
                              "description": "Para escopo='mp', lista so MPs abaixo do minimo."},
        },
        "required": ["escopo"],
    },
}

TOOL_RECEBER_MP = {
    "name": "receber_mp",
    "description": "Registra entrada de materia-prima no estoque. NAO executa direto — retorna preview.",
    "input_schema": {
        "type": "object",
        "properties": {
            "mp_nome": {"type": "string", "description": "Nome EXATO da MP do catalogo."},
            "quantidade": {"type": ["number", "null"], "minimum": 0.01, "description": "Quantidade NA UNIDADE DO CADASTRO da MP (un/g/ml). NAO passe kg aqui."},
            "quantidade_kg": {"type": ["number", "null"], "minimum": 0.001, "description": "Peso TOTAL em kg, quando a NF vem em kg/sacos. MP em 'un' com peso por unidade: converte pra unidades (ex: 8 kg de bolinhas de 18g = 444 un). MP em g/ml: converte x1000. Use quantidade OU quantidade_kg, nunca os dois."},
            "preco_total": {"type": ["number", "null"], "description": "Valor total pago (R$). Calcula preco_unitario automaticamente."},
            "preco_unitario": {"type": ["number", "null"], "description": "Preco por unidade. Alternativa ao preco_total."},
            "referencia": {"type": ["string", "null"], "description": "Ex: 'NF 12345 - Fornecedor X'."},
        },
        "required": ["mp_nome"],
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
    "description": ("Muda o status de um pedido entre lojas: confirmar (pendente→confirmado), "
                    "separar (→separado), enviar (→em_transporte, baixa estoque industria), "
                    "receber (→recebido, soma no estoque da loja, sem divergencias), "
                    "ou cancelar. NAO executa — preview."),
    "input_schema": {
        "type": "object",
        "properties": {
            "pedido_id": {"type": "integer"},
            "novo_status": {"type": "string", "enum": ["confirmar", "separar", "enviar", "receber", "cancelar"]},
        },
        "required": ["pedido_id", "novo_status"],
    },
}

TOOL_ENVIAR_DIGEST_WHATSAPP = {
    "name": "enviar_digest_whatsapp",
    "description": ("Envia uma mensagem WhatsApp pro numero configurado em "
                    "ZAPI_NUMERO_DESTINO. Use sempre que o usuario disser "
                    "'manda no whatsapp', 'envia no zap', 'me lembra das "
                    "tarefas no whatsapp', 'faz um teste no whatsapp'. "
                    "Se `texto_custom` for fornecido, envia esse texto. "
                    "Caso contrario, envia o digest de tarefas (hoje + atrasadas)."),
    "input_schema": {
        "type": "object",
        "properties": {
            "texto_custom": {"type": ["string", "null"],
                              "description": "Texto personalizado a enviar. Null = manda o digest de tarefas."},
        },
    },
}


TOOL_RECEBER_PEDIDO = {
    "name": "receber_pedido",
    "description": ("Marca um pedido como recebido na loja (em_transporte→recebido). "
                    "Soma no estoque da loja, sem divergencias (qtd igual ao pedido). "
                    "NAO executa — preview. Use quando o usuario disser 'recebi pedido X', "
                    "'pedido X chegou', 'confirma recebimento do X'."),
    "input_schema": {
        "type": "object",
        "properties": {
            "pedido_id": {"type": "integer"},
        },
        "required": ["pedido_id"],
    },
}

TOOL_ANEXAR_FOTO_PEDIDO = {
    "name": "anexar_foto_pedido",
    "description": ("Anexa foto(s) de comprovante a um pedido (ex: foto da entrega, nota fiscal). "
                    "As imagens vem da mensagem do usuario no Slack. NAO executa — preview."),
    "input_schema": {
        "type": "object",
        "properties": {
            "pedido_id": {"type": "integer"},
        },
        "required": ["pedido_id"],
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

TOOL_PREVER_PEDIDO = {
    "name": "prever_pedido",
    "description": (
        "PREVISAO DE PEDIDO DE REPOSICAO por loja, calculada server-side a "
        "partir do historico de pedidos das lojas (PedidoLoja). Use SEMPRE "
        "que o usuario pedir 'previsao de pedido', 'quanto pedir pra semana "
        "que vem', 'previsao das lojas', 'baseado nas ultimas N semanas'. "
        "NAO faca essa conta na mao com consultar_pedido — esta tool ja soma "
        "as quantidades por item e divide pelas semanas. Retorna, por loja, a "
        "lista de itens com total no periodo, media semanal e 'sugerido' (o "
        "que pedir pra proxima semana). Sem `loja`, devolve TODAS as lojas com "
        "pedido no periodo (use pra 'as 3 lojas'). Apresente o RESULTADO "
        "(lista sugerida por loja), nunca o historico cru."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "semanas": {"type": ["integer", "null"],
                        "description": "Quantas semanas de historico considerar (default 3, max 12)."},
            "loja": {"type": ["string", "null"],
                     "description": "Nome da loja (opcional). Vazio = todas as lojas com pedido no periodo."},
            "data_ref": {"type": ["string", "null"],
                         "description": "YYYY-MM-DD de referencia (default hoje). A janela vai de data_ref - semanas*7 ate data_ref."},
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

TOOL_DEVOLVER_INDUSTRIA = {
    "name": "devolver_industria",
    "description": (
        "Devolve sobras de uma loja pra INDUSTRIA (duas pontas: baixa o "
        "estoque da loja E credita o congelado da industria no mesmo ato — "
        "na receita de retorno quando configurada, ex: Croissant Tradicional "
        "vira 'Croissant Tradicional — Retorno', que o Croissant Almond "
        "consome). Use quando o usuario disser 'voltaram X pra industria', "
        "'mandei as sobras de volta', 'devolvi N croissants'. NAO e "
        "desperdicio (nada vai pro lixo) nem venda. NAO executa direto — "
        "retorna preview pra aprovar."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "loja_id": {"type": ["integer", "null"], "description": "ID da loja que devolve."},
            "loja_nome": {"type": ["string", "null"], "description": "Nome da loja (fuzzy match). Use loja_id quando souber."},
            "itens": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "nome": {"type": "string", "description": "Nome da receita/produto devolvido. Aceita aproximacoes (fuzzy match)."},
                        "quantidade": {"type": "integer", "minimum": 1, "description": "Quantidade devolvida."},
                    },
                    "required": ["nome", "quantidade"],
                },
                "minItems": 1,
            },
        },
        "required": ["itens"],
    },
}

TOOL_CRIAR_RETIRADA_SOBRAS = {
    "name": "criar_retirada_sobras",
    "description": (
        "Cria um PEDIDO DE RETIRADA de sobras reaproveitaveis da loja pra "
        "industria no dia seguinte (ex: croissants que vao virar Almond). "
        "EXIGE FOTO da sobra, mas foto e quantidade podem vir em MENSAGENS "
        "SEPARADAS: o sistema anexa automaticamente a foto da mensagem atual "
        "OU a ultima foto que o usuario mandou no canal (2h). Fluxo: apos "
        "registrar sobras, se algum item reaproveitavel tem receita de "
        "retorno, PERGUNTE quantos voltam pra industria; assim que souber a "
        "quantidade, chame esta tool — NAO exija a foto de novo se o usuario "
        "ja mandou alguma nesta conversa; so peca foto se nenhuma foi "
        "enviada. O motorista coleta no dia seguinte via QR code (esteira "
        "igual as entregas: coleta baixa a loja, recebimento credita a "
        "industria). NAO executa direto — preview pra aprovar."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "loja_id": {"type": ["integer", "null"], "description": "ID da loja."},
            "loja_nome": {"type": ["string", "null"], "description": "Nome da loja (fuzzy). Use loja_id quando souber."},
            "itens": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "nome": {"type": "string", "description": "Nome da receita (fuzzy match)."},
                        "quantidade": {"type": "integer", "minimum": 1, "description": "Quantos voltam pra industria."},
                    },
                    "required": ["nome", "quantidade"],
                },
                "minItems": 1,
            },
            "observacao": {"type": ["string", "null"]},
        },
        "required": ["itens"],
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
            "motivo": {"type": "string", "enum": ["validade", "nao_vendeu", "estragou", "caiu", "queimou", "outro"], "description": "Default 'validade'. Use 'nao_vendeu' quando o usuario disser 'sobra do dia', 'sobrou', 'nao vendeu'. Itens marcados como reaproveitaveis no cadastro NAO baixam estoque quando motivo='validade' OU 'nao_vendeu' (vence/sobra mas vira outra coisa: croissant tradicional vencido vira almond, sourdough tradicional sobra vira chapa). Outros motivos sempre baixam. Sinonimos: 'vencido'=validade, 'estragado'=estragou, 'queimado'=queimou."},
            "observacao": {"type": ["string", "null"]},
        },
        "required": ["item_nome", "quantidade"],
    },
}

TOOL_REGISTRAR_DESPERDICIO_LOTE = {
    "name": "registrar_desperdicio_lote",
    "description": (
        "Registra varios itens de desperdicio (sobra do dia/vencido/etc) "
        "de uma loja de uma vez. Use SEMPRE que o usuario passar uma LISTA "
        "de itens vencidos/descartados ('anota essas sobras: 2 croissants, "
        "3 pao frances, 1 nutella...'). Para 1 item so, use registrar_desperdicio. "
        "Baixa do estoque da loja. NAO executa direto — retorna preview pra aprovar. "
        "CRITICO: se um lote acabou de ser registrado e FALTOU um item, chame "
        "de novo APENAS com o item que faltou — reenviar a lista inteira "
        "DUPLICA as perdas dos itens ja registrados."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "loja_id": {"type": ["integer", "null"]},
            "loja_nome": {"type": ["string", "null"], "description": "Nome da loja (ex: 'Ribeiro do Vale'). SEMPRE passe loja_nome OU loja_id — fuzzy match no servidor. Se o usuario nao mencionou a loja, NAO chame a tool: pergunte primeiro."},
            "motivo": {"type": "string", "enum": ["validade", "nao_vendeu", "estragou", "caiu", "queimou", "outro"], "description": "Motivo unico pro lote inteiro. Default 'validade'. Use 'nao_vendeu' pra sobra do dia. Itens marcados como reaproveitaveis no cadastro NAO baixam estoque quando motivo='validade' OU 'nao_vendeu' (item vence/sobra mas vira outra coisa)."},
            "itens": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "nome": {"type": "string", "description": "Nome do item (receita/produto/MP). Fuzzy match no servidor."},
                        "quantidade": {"type": "integer", "minimum": 1},
                        "observacao": {"type": ["string", "null"], "description": "Obs especifica desse item (opcional)."},
                    },
                    "required": ["nome", "quantidade"],
                },
                "minItems": 1,
            },
            "observacao": {"type": ["string", "null"], "description": "Obs geral do lote (opcional)."},
        },
        "required": ["itens"],
    },
}

TOOL_CRIAR_CLIENTE_B2B = {
    "name": "criar_cliente_b2b",
    "description": (
        "Cadastra um novo cliente B2B (hotel, restaurante, cafeteria). "
        "Use quando o usuario disser 'cadastra cliente X' ou quando "
        "tentar criar venda B2B pra cliente novo. NAO executa direto — "
        "retorna preview pra confirmar."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "nome": {"type": "string", "description": "Nome do cliente (ex: 'Hotel Brisamar', 'Restaurante do Joao')."},
            "cnpj_cpf": {"type": ["string", "null"]},
            "telefone": {"type": ["string", "null"]},
            "email": {"type": ["string", "null"]},
            "endereco": {"type": ["string", "null"]},
            "contato": {"type": ["string", "null"], "description": "Nome da pessoa contato."},
            "desconto_percentual": {"type": ["number", "null"], "minimum": 0, "maximum": 100, "description": "% de desconto sobre preco atacado. Default 0."},
            "observacao": {"type": ["string", "null"]},
        },
        "required": ["nome"],
    },
}

TOOL_CRIAR_VENDA_B2B = {
    "name": "criar_venda_b2b",
    "description": (
        "Cria venda B2B (industria → cliente externo). Com data de "
        "entrega, o pedido entra na fila do padeiro e o estoque do "
        "FREEZER (EstoqueProducao) so baixa quando o padeiro SEPARAR "
        "no /padeiro; venda imediata (sem data) baixa na hora. Pode ser "
        "cliente cadastrado (cliente_nome resolve por fuzzy match) ou "
        "avulso. Se cliente nao existir, sugira `criar_cliente_b2b` "
        "antes OU passe so cliente_nome (vira venda avulsa). NAO executa "
        "direto — preview com itens, total, parcelas. Preco vem do "
        "cadastro (Receita.preco_venda / Produto.preco_atacado) se nao "
        "especificado."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "cliente_nome": {"type": "string", "description": "Nome do cliente. Servidor faz fuzzy match com cadastrados; se nao achar, fica como avulso."},
            "data_venda": {"type": ["string", "null"], "description": "YYYY-MM-DD. Default hoje."},
            "data_entrega": {"type": "string", "description": "YYYY-MM-DD. OBRIGATORIO: dia que a venda vai pra fila do padeiro produzir/separar. Pergunte se o usuario nao disser. NAO confundir com loja (B2B nao tem loja)."},
            "nf_numero": {"type": ["string", "null"]},
            "observacao": {"type": ["string", "null"]},
            "frete_valor": {"type": ["number", "null"], "minimum": 0, "description": "Frete da entrega em R$ (soma no total; vai no boleto e no campo frete da NF). Null/0 = sem frete."},
            "itens": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "nome": {"type": "string", "description": "Nome EXATO da receita/produto, SEM o estado. Ex: 'Croissant Tradicional'."},
                        "estado": {"type": ["string", "null"], "enum": [None, "cru", "backup", "assado"], "description": "Estado p/ producao. 'Croissant backup' => nome 'Croissant Tradicional' + estado 'backup'. Default cru (null)."},
                        "quantidade": {"type": "integer", "minimum": 1},
                        "preco_unitario": {"type": ["number", "null"], "description": "Sobrescreve preco do cadastro. Null = usa cadastro."},
                        "desconto_percentual": {"type": ["number", "null"], "minimum": 0, "maximum": 100},
                    },
                    "required": ["nome", "quantidade"],
                },
                "minItems": 1,
            },
            "parcelas": {
                "type": ["array", "null"],
                "description": "Parcelas. Null/vazio = 1 parcela unica no dia da venda pelo total. Se explicitas, a SOMA deve fechar o total da venda INCLUINDO o frete (itens + frete_valor) — nada valida por voce.",
                "items": {
                    "type": "object",
                    "properties": {
                        "vencimento": {"type": "string", "description": "YYYY-MM-DD"},
                        "valor": {"type": "number", "minimum": 0.01},
                        "forma_pagamento": {"type": ["string", "null"], "enum": [None, "pix", "boleto", "dinheiro", "transferencia", "cheque"]},
                    },
                    "required": ["vencimento", "valor"],
                },
            },
        },
        "required": ["cliente_nome", "data_entrega", "itens"],
    },
}

TOOL_CONSULTAR_CLIENTE_B2B = {
    "name": "consultar_cliente_b2b",
    "description": "Lista/busca clientes B2B cadastrados. Use pra ver se cliente existe antes de criar venda, ou pra info de contato/desconto. Filtra por nome substring opcional.",
    "input_schema": {
        "type": "object",
        "properties": {
            "nome": {"type": ["string", "null"], "description": "Filtro por nome (substring). Null = lista todos ativos."},
        },
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

TOOL_CONSULTAR_CATALOGO_SITE = {
    "name": "consultar_catalogo_site",
    "description": ("Busca produtos/cestas no catalogo do SITE (VNDA) por "
                     "nome — retorna NOME, SKU, PRECO, disponibilidade e "
                     "URL DA PAGINA do produto. Use sempre que o usuario "
                     "pedir 'me manda o link da cesta X', 'qual o link de Y', "
                     "ou quiser ver preco/disponibilidade no SITE (nao no "
                     "PDV — pra isso e' consultar_vendas_itens)."),
    "input_schema": {
        "type": "object",
        "properties": {
            "busca": {"type": "string",
                       "description": "Termo livre. Ex: 'cesta dia dos namorados', 'sourdough'."},
        },
        "required": ["busca"],
    },
}

TOOL_CONSULTAR_CARTINHAS = {
    "name": "consultar_cartinhas",
    "description": ("Lista cartinhas cadastradas/editadas no painel de "
                     "entregas num periodo. Cada cartinha = cliente que "
                     "pediu mensagem personalizada no pedido (Dia das Maes, "
                     "Namorados, etc.). Retorna pedido_code, texto, quem "
                     "cadastrou e quando."),
    "input_schema": {
        "type": "object",
        "properties": {
            "dias": {"type": "integer", "minimum": 1, "maximum": 30,
                      "description": "Janela em dias (default 2)."},
        },
    },
}

TOOL_CONSULTAR_VIGIA = {
    "name": "consultar_vigia",
    "description": (
        "Le o que a Vigia do chatbot do site flagrou — reclamacoes "
        "operacionais, handoffs preguicosos, 'bot delirou', clientes "
        "esperando humano. Cada alerta que chega no WhatsApp do dono "
        "vem daqui. Use quando o dono perguntar 'qual foi a reclamacao', "
        "'o que aconteceu na conversa X', 'me da detalhes do alerta', "
        "'quem reclamou hoje'. Default: ultimos 1 dia, gravidade alta. "
        "Passe conv_id pra ver TODOS os vereditos de uma conversa "
        "especifica (com a mensagem do cliente que disparou cada um). "
        "Passe `palavra` pra filtrar por texto na mensagem do cliente "
        "ou no motivo da vigia (ex: 'reclamacao', 'esperando', 'cesta')."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "dias": {"type": "integer", "minimum": 1, "maximum": 14,
                      "description": "Janela em dias. Default 1 (ultimas 24h)."},
            "gravidade": {"type": ["string", "null"],
                          "enum": ["alta", "media", None],
                          "description": ("'alta' (default), 'media' ou null pra "
                                          "TODOS — incluindo 'baixa'/sem gravidade.")},
            "conv_id": {"type": ["string", "null"],
                         "description": ("ID da conversa no Chatwoot. Quando setado, "
                                         "ignora `dias`/`gravidade` e retorna todo o "
                                         "historico de vereditos dessa conversa.")},
            "palavra": {"type": ["string", "null"],
                         "description": ("Filtro de texto (substring, case-insensitive) "
                                         "em mensagem_cliente OU motivo_vigia.")},
        },
    },
}

TOOL_CONSULTAR_CONVERSA_CHATWOOT = {
    "name": "consultar_conversa_chatwoot",
    "description": (
        "Le o HISTORICO de uma conversa especifica no Chatwoot — as "
        "mensagens reais entre cliente, bot e atendente. Use quando o "
        "dono perguntar 'me mostra a conversa #X', 'o que foi falado', "
        "'o que o cliente disse'. O conv_id vem do alerta da Vigia "
        "(ex: 'conversa #198') ou do consultar_vigia."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "conv_id": {"type": "string",
                         "description": "ID da conversa no Chatwoot."},
            "limite": {"type": "integer", "minimum": 1, "maximum": 50,
                        "description": ("Quantas mensagens recentes trazer. "
                                        "Default 20.")},
        },
        "required": ["conv_id"],
    },
}

TOOL_LISTAR_CONVERSAS_CHATWOOT = {
    "name": "listar_conversas_chatwoot",
    "description": (
        "Lista conversas ATIVAS no Chatwoot — quem ta na fila do bot "
        "(status=pending) ou quem ta esperando atendente humano "
        "(status=open). Use quando o dono perguntar 'quem ta esperando', "
        "'tem alguem na fila', 'lista as conversas paradas'. Retorna "
        "id, nome do contato e ha quantos minutos a conversa ta parada."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {"type": "string",
                        "enum": ["pending", "open"],
                        "description": ("'pending' = aguardando bot, "
                                        "'open' = humano assumiu. Default 'open' "
                                        "(o que mais importa: cliente esperando atendente).")},
            "min_minutos": {"type": "integer", "minimum": 0, "maximum": 1440,
                             "description": ("So lista conversa parada ha pelo menos "
                                             "X minutos. Default 0 (todas).")},
        },
    },
}

# ── Memoria persistente (notas markdown — 15/06/2026) ────────────────
# Substitui a "memoria efemera" que evaporava a cada sessao. O dono ensina
# uma regra/excecao/decisao via copilot → registrar_nota → consulta nas
# proximas conversas. Mesma tabela serve o bot Padeiro (Chatwoot) — bot so
# le, copilot le e escreve.
TOOL_REGISTRAR_NOTA = {
    "name": "registrar_nota",
    "description": (
        "Grava uma nota PERSISTENTE quando o usuario te ensinar uma REGRA, "
        "EXCECAO, DECISAO de negocio, ou CONHECIMENTO que deve ser lembrado "
        "nas proximas conversas. Exemplos: 'a partir de hoje cookies cortam "
        "em 5', 'loja Anesio nao vende croissant nutella', 'fornecedor X "
        "sempre atrasa entrega na sexta', 'quando cliente pedir cesta de "
        "natal, ja oferece o Box Mimo'. NAO use pra dados efemeros (status "
        "de pedido especifico, lembrete de tarefa do dia). Confirme no texto "
        "que registrou, pra o usuario corrigir se entendeu errado. Notas "
        "ficam visiveis em /notas — admin edita/arquiva la."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "titulo": {"type": "string",
                       "description": "Frase curta resumindo a regra (max 200 chars)"},
            "conteudo": {"type": "string",
                          "description": "A regra COMPLETA em markdown. "
                          "Inclua contexto, exececoes, exemplos."},
            "tags": {"type": "string",
                      "description": "Tags separadas por virgula pra ajudar a "
                      "buscar depois. Ex: 'cookie,corte,cafe'. Opcional mas "
                      "recomendado."},
        },
        "required": ["titulo", "conteudo"],
    },
}

TOOL_CONSULTAR_NOTAS = {
    "name": "consultar_notas",
    "description": (
        "Busca nas notas persistentes (regras/excecoes de negocio que voce ou "
        "outros usuarios ensinaram em sessoes anteriores). USE ANTES de "
        "responder perguntas de NEGOCIO cuja resposta pode estar em uma regra "
        "previamente cadastrada — assim voce nao 'esquece' o que combinaram. "
        "Termo curto/vazio devolve as mais recentes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "termo": {"type": "string",
                       "description": "Palavras-chave pra buscar (titulo, tags "
                       "e conteudo). Vazio = mais recentes."},
        },
    },
}


TOOLS = [
    # Existentes
    TOOL_CRIAR_PEDIDO, TOOL_EDITAR_PEDIDO, TOOL_CONSULTAR_PEDIDO, TOOL_CONSULTAR_ESTOQUE,
    TOOL_RECEBER_MP, TOOL_AJUSTE_ESTOQUE,
    # Novas — acoes operacionais
    TOOL_MUDAR_STATUS_PEDIDO, TOOL_RECEBER_PEDIDO, TOOL_ANEXAR_FOTO_PEDIDO,
    TOOL_ENVIAR_DIGEST_WHATSAPP,
    TOOL_CRIAR_FORNECEDOR, TOOL_MARCAR_PONTO,
    TOOL_CRIAR_TAREFA,
    # Novas — consultas
    TOOL_CONSULTAR_FORNECEDORES, TOOL_CONSULTAR_MARGEM,
    TOOL_CONSULTAR_FUNCIONARIO, TOOL_CONSULTAR_CAIXA,
    TOOL_CONSULTAR_VENDAS_ITENS,
    TOOL_PREVER_PEDIDO,
    # Planejamento
    TOOL_CONSULTAR_FOCO, TOOL_CONSULTAR_TAREFAS, TOOL_MARCAR_TAREFA_FEITA,
    # Estoque de congelados / loja
    TOOL_BALANCO_CONGELADOS, TOOL_ENTRADA_LOTE_LOJA,
    # Devolucao de sobras loja -> industria (duas pontas)
    TOOL_DEVOLVER_INDUSTRIA, TOOL_CRIAR_RETIRADA_SOBRAS,
    # Desperdicio (sobra do dia / vencido)
    TOOL_REGISTRAR_DESPERDICIO, TOOL_REGISTRAR_DESPERDICIO_LOTE,
    TOOL_CONSULTAR_DESPERDICIO,
    TOOL_CONSULTAR_CATALOGO_SITE,
    TOOL_CONSULTAR_CARTINHAS,
    # Visibilidade do chatbot do site (Vigia + Chatwoot) — owner-only
    TOOL_CONSULTAR_VIGIA, TOOL_CONSULTAR_CONVERSA_CHATWOOT,
    TOOL_LISTAR_CONVERSAS_CHATWOOT,
    # B2B (venda industria pra cliente externo)
    TOOL_CRIAR_CLIENTE_B2B, TOOL_CRIAR_VENDA_B2B, TOOL_CONSULTAR_CLIENTE_B2B,
    # Memoria persistente (15/06/2026)
    TOOL_REGISTRAR_NOTA, TOOL_CONSULTAR_NOTAS,
]

# Quais tools requerem preview/aprovacao (writes)
REQUER_APROVACAO = {
    'criar_pedido', 'editar_pedido', 'receber_mp', 'ajuste_estoque',
    'mudar_status_pedido', 'criar_fornecedor', 'marcar_ponto', 'criar_tarefa',
    'marcar_tarefa_feita', 'balanco_congelados', 'entrada_lote_loja',
    'devolver_industria', 'criar_retirada_sobras',
    'registrar_desperdicio', 'registrar_desperdicio_lote',
    'anexar_foto_pedido', 'receber_pedido',
    'criar_cliente_b2b', 'criar_venda_b2b',
}


# ── PERMISSOES POR TOOL ────────────────────────────────────────────────
#
# Esta matriz governa quem pode invocar cada tool do copilot. Ela eh
# SEPARADA dos decorators de rota (admin_required, gerente_required, etc.)
# em `app/decorators.py` que protegem URLs HTTP — o copilot pode ser
# chamado de qualquer rota autenticada, entao precisa do proprio gate.
#
# Quatro papeis canonicos (`papel_efetivo()`):
# - 'owner'       : dono unico — superconjunto de admin; unico que usa as
#                   tools marcadas {'owner'} (RH: ponto, consultar funcionario)
# - 'admin'       : ve/faz quase tudo (menos as tools {'owner'})
# - 'gerente'     : operacao de loja (pedidos, estoque, vendas, ajustes)
# - 'funcionario' : tarefas, consultas basicas, registrar desperdicio
#
# Default (nao listado em PAPEIS_POR_TOOL) = SO ADMIN. Princípio do
# menor privilegio — se voce esquecer de mapear uma tool nova, ela
# fica restrita ao admin por seguranca.
#
# Pra adicionar tool nova:
#   1. Definir TOOL_X = {...}
#   2. Adicionar handler em _READ_HANDLERS / _EXEC_HANDLERS
#   3. Adicionar entrada em PAPEIS_POR_TOOL (mesmo que seja so admin —
#      explicitar evita confusao)
#   4. Adicionar teste em tests/test_copilot_permissoes.py (cobre regressao)
PAPEIS_POR_TOOL = {
    # Operacao geral — admin + gerente
    'criar_pedido': {'admin', 'gerente'},
    'editar_pedido': {'admin', 'gerente'},
    'mudar_status_pedido': {'admin', 'gerente', 'producao'},
    'receber_mp': {'admin', 'gerente'},
    'ajuste_estoque': {'admin', 'gerente'},
    # RH — owner-only (espelha o RH web, restrito ao owner)
    'marcar_ponto': {'owner'},
    # Cadastros — so admin
    'criar_fornecedor': {'admin'},
    'consultar_margem': {'admin'},
    # Balanco de congelados — so admin (sobrescreve estoque)
    'balanco_congelados': {'admin'},
    # Entrada em lote no estoque de loja — so admin
    'entrada_lote_loja': {'admin'},
    # Devolucao de sobras loja -> industria (duas pontas) — quem opera loja
    'devolver_industria': {'admin', 'gerente'},
    # Retirada de sobras (QR, dia seguinte) — mesmo publico do desperdicio:
    # e a continuacao natural do lancamento de sobras da noite.
    'criar_retirada_sobras': {'admin', 'gerente', 'funcionario'},
    # Consultas operacionais — admin + gerente
    'consultar_fornecedores': {'admin', 'gerente'},
    # RH — owner-only (espelha o RH web, restrito ao owner)
    'consultar_funcionario': {'owner'},
    'consultar_caixa': {'admin', 'gerente'},
    'consultar_vendas_itens': {'admin', 'gerente'},
    'prever_pedido': {'admin', 'gerente'},
    'consultar_desperdicio': {'admin', 'gerente', 'funcionario'},
    'consultar_catalogo_site': {'admin', 'gerente', 'funcionario'},
    'consultar_cartinhas': {'admin', 'gerente'},
    # Vigia + Chatwoot: owner-only (mensagens de cliente / alertas operacionais)
    'consultar_vigia': {'owner'},
    'consultar_conversa_chatwoot': {'owner'},
    'listar_conversas_chatwoot': {'owner'},
    'enviar_digest_whatsapp': {'admin'},
    'registrar_desperdicio': {'admin', 'gerente', 'funcionario'},
    'registrar_desperdicio_lote': {'admin', 'gerente', 'funcionario'},
    'criar_cliente_b2b': {'admin'},
    'criar_venda_b2b': {'admin'},
    'consultar_cliente_b2b': {'admin', 'gerente'},
    'anexar_foto_pedido': {'admin', 'gerente', 'funcionario'},
    'receber_pedido': {'admin', 'gerente', 'funcionario'},
    # Consultas + planejamento — todos
    'consultar_pedido': {'admin', 'gerente', 'funcionario'},
    'consultar_estoque': {'admin', 'gerente', 'funcionario'},
    'consultar_foco': {'admin', 'gerente', 'funcionario'},
    'consultar_tarefas': {'admin', 'gerente', 'funcionario'},
    'criar_tarefa': {'admin', 'gerente', 'funcionario'},
    'marcar_tarefa_feita': {'admin', 'gerente', 'funcionario'},
    # Memoria persistente: leitura aberta (qualquer um precisa lembrar das
    # regras), escrita restrita a quem define regra de negocio (owner e
    # admin). Sem isso, vira lixo dump rapido.
    'consultar_notas': {'admin', 'gerente', 'funcionario'},
    'registrar_nota': {'admin'},
}


def papel_efetivo(user):
    """Mapeia user → string de papel canonico pra checagem."""
    if not user or not getattr(user, 'is_authenticated', False):
        return None
    # Owner antes de admin: is_admin() inclui o owner, entao testamos
    # is_dono() primeiro pra distinguir o tier owner-only (RH).
    if user.is_dono():
        return 'owner'
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
    # Owner/admin sempre full — nao entram na matriz editavel (sem lockout).
    if papel == 'owner':
        return 'owner' in permitidos or 'admin' in permitidos
    if papel == 'admin':
        return 'admin' in permitidos
    # Demais papeis: se a tool for editavel, consulta o modelo unificado
    # (app/services/permissoes.py) pelo PAPEL REAL. Senao, default fixo do codigo.
    from app.services import permissoes
    if permissoes.eh_editavel(tool_name):
        return permissoes.pode((getattr(user, 'papel', '') or 'funcionario'), tool_name)
    return papel in permitidos


def tools_permitidas(user):
    """Lista tools que o user pode usar — vai pro Claude no system prompt
    pra ele nao tentar tools que vao ser rejeitadas."""
    return [t for t in TOOLS if pode_usar(t['name'], user)]


# Cache do catalogo (5 queries) — TTL curto pra absorver picos de uso do
# copilot sem refazer queries a cada chamada. 60s eh suficiente: o catalogo
# raramente muda, e novo produto/receita aparece pro copilot em ate 1 min.
_CATALOGO_CACHE = {'texto': None, 'expira_em': 0.0}
_CATALOGO_TTL = 60  # segundos


def _catalogo_texto():
    """Lista produtos + receitas + MPs + fornecedores + funcionarios
    formatados pra contexto do LLM. Cacheado por _CATALOGO_TTL segundos."""
    import time
    now = time.time()
    if _CATALOGO_CACHE['texto'] and _CATALOGO_CACHE['expira_em'] > now:
        return _CATALOGO_CACHE['texto']

    from app.models import Fornecedor, Funcionario
    linhas = ["PRODUTOS (use o nome exato):"]
    # ativo=True: produto desativado nao pode ser OFERECIDO como opcao pelo
    # modelo (varredura 19/07/2026 — Receita/MP/Fornecedor ja filtravam).
    for p in Produto.query.filter(Produto.ativo.is_(True)) \
                          .order_by(Produto.nome).all():
        linhas.append(f"  - {p.nome}")
    linhas.append("")
    linhas.append("RECEITAS (use o nome exato):")
    for r in Receita.query.filter(Receita.arquivada_em.is_(None)).order_by(Receita.nome).all():
        linhas.append(f"  - {r.nome}")
    linhas.append("")
    linhas.append("MATERIAS PRIMAS (use o nome exato):")
    for m in MateriaPrima.ativas().order_by(MateriaPrima.nome).all():
        unidade = m.unidade or '?'
        linhas.append(f"  - {m.nome} ({unidade})")
    linhas.append("")
    linhas.append("FORNECEDORES ATIVOS:")
    fornecedores = Fornecedor.query.filter_by(ativo=True).order_by(Fornecedor.nome).all()
    if fornecedores:
        for f in fornecedores:
            linhas.append(f"  - {f.nome}")
    else:
        linhas.append("  (nenhum cadastrado)")
    linhas.append("")
    linhas.append("FUNCIONARIOS ATIVOS:")
    funcs = (Funcionario.query.filter_by(ativo=True)
             .order_by(Funcionario.nome).limit(80).all())
    if funcs:
        for f in funcs:
            funcao = f.funcao or f.funcao_operacional or '?'
            linhas.append(f"  - {f.nome} ({funcao})")
    else:
        linhas.append("  (nenhum cadastrado)")

    texto = "\n".join(linhas)
    _CATALOGO_CACHE['texto'] = texto
    _CATALOGO_CACHE['expira_em'] = now + _CATALOGO_TTL
    return texto


def invalidar_catalogo_cache():
    """Invalida cache do catalogo. Chamar quando produto/receita/MP/fornecedor
    /funcionario for criado/editado/desativado."""
    _CATALOGO_CACHE['texto'] = None
    _CATALOGO_CACHE['expira_em'] = 0.0


def _lojas_texto(user):
    """Lista todas as lojas operacionais (ativas, sem Industria) — todos os
    usuarios podem atuar em qualquer loja, entao listamos tudo."""
    lojas = (Loja.query
             .filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
             .order_by(Loja.nome).all())
    if not lojas:
        return "(nenhuma loja cadastrada)"
    return "\n".join(f"  - id={l.id}: {l.nome}" for l in lojas)


def _build_system_prompt(user, tools_visiveis=None, apenas_leitura=False):
    from app.utils import hoje as _hoje_brt
    hoje = _hoje_brt().isoformat()
    papel = papel_efetivo(user) or 'desconhecido'
    nome_user = getattr(user, 'nome', None) or getattr(user, 'login', '?')
    # Lista de tools que ESTE user pode usar — Claude ja recebe filtrado,
    # mas dizer explicitamente evita alucinacao tipo "essa tool nao esta
    # disponivel pra mim" quando na verdade eh permissao do usuario.
    # IMPORTANTE: a lista do prompt tem que ser a MESMA enviada na API —
    # caso real (bot WhatsApp read-only, 11/06/2026): o prompt listava
    # criar_tarefa (write) que o filtro apenas_leitura tinha removido, e o
    # bot PROMETIA "tenho a tool criar_tarefa" sem conseguir invoca-la.
    if tools_visiveis is None:
        tools_visiveis = tools_permitidas(user)
    tools_do_user = sorted([t['name'] for t in tools_visiveis])
    aviso_leitura = ''
    if apenas_leitura:
        aviso_leitura = """
MODO SOMENTE LEITURA (canal WhatsApp do dono):
- Voce SO consulta. Nao existe nenhuma tool de criar/editar/registrar aqui.
- Se pedirem uma acao (criar tarefa, pedido, ajuste...), NAO prometa fazer:
  diga que pelo WhatsApp voce so consulta, e que acoes sao feitas pelo bot
  do Slack ou direto no sistema.
- ATENCAO: 'modo leitura' NAO significa 'sem memoria'. Voce CONTINUA tendo
  acesso ao historico desta conversa (ver regra MEMORIA abaixo).
"""
    return f"""Voce e' um assistente de gestao de uma padaria. Interpreta comandos em
linguagem natural e estrutura acoes pra o usuario confirmar.

Hoje e' {hoje}.

USUARIO LOGADO: {nome_user} (papel: {papel}).
TOOLS QUE ESTE USUARIO PODE USAR: {', '.join(tools_do_user) if tools_do_user else '(nenhuma)'}.

MEMORIA — CRITICA:
- Voce TEM acesso ao historico completo desta conversa multi-turn (ate 80
  mensagens anteriores) — elas vem na sua janela de contexto antes desta.
- NUNCA diga "esqueci", "nao lembro", "cada sessao comeca do zero", "nao
  tenho acesso ao historico anterior", "nao consigo ver conversas
  anteriores". Isso e' falso e quebra a confianca do usuario.
- Quando o usuario disser "manda esse link de novo", "como voce me disse",
  "o que voce falou ontem", PROCURE no historico ACIMA antes de responder.
  Se mesmo procurando voce realmente nao achar, diga "Procurei aqui na
  nossa conversa e nao achei essa mensagem — me ajuda a lembrar do
  contexto?", NUNCA "nao tenho memoria".
{aviso_leitura}
REGRA DE PERMISSAO — CRITICA:
- Se uma acao que o usuario pediu NAO esta na lista acima, NAO INVENTE
  "limitacao do meu acesso", "tool nao disponivel pra mim", "estou offline",
  ou similares — isso confunde o usuario.
- Diga claramente: "Voce e' {papel}. A acao [criar_pedido / etc] precisa
  de papel admin ou gerente. Peca a um admin do sistema (ex: Caio) pra
  fazer, ou pedir que ele altere seu papel pra gerente em /auth/usuarios."
- Tools que exigem admin/gerente que NAO funcionarios podem usar:
  criar_pedido, receber_mp, ajuste_estoque, criar_fornecedor, criar_venda_b2b.

REGRA ANTI-SUBSTITUICAO — CRITICA:
- NUNCA use uma tool como substituto de outra. Exemplos PROIBIDOS:
  * Usuario pede "criar pedido" mas voce nao tem criar_pedido → NAO crie
    criar_tarefa com titulo "Criar pedido X". Isso gera tarefa fantasma e
    o pedido NAO eh criado de verdade. Diga o motivo e pare.
  * Usuario pede "registrar desperdicio" e voce nao tem → nao crie tarefa.
  * Usuario pede "receber pedido" → nao registre desperdicio.
- Se a tool certa nao esta disponivel, NUNCA crie nada parecido. Responda
  em texto: "Voce e' {papel} e essa acao precisa de admin/gerente. Peca
  a um admin (Caio) pra fazer manualmente." Ponto final. Nao crie tarefa.

LOJAS DISPONIVEIS:
{_lojas_texto(user)}

{_catalogo_texto()}

PADRAO DE INTENCAO — CRITICO:
- Quando o usuario manda uma lista de itens com **loja + data** (mesmo sem
  dizer "criar pedido" explicitamente), a intencao DEFAULT eh criar_pedido.
  NAO interpretar como tarefa, lembrete, ou anotacao.
  Exemplos que DEVEM virar criar_pedido (nao tarefa):
    "Ribeiro do Vale | 22/05: 50 Croissant, 15 Cinnamon Roll, 20 Brioche"
    "Anesio amanha: 30 paes, 10 cookies"
    "pedido pra Nebraska sexta 100 croissant, 50 brioche"
  Se voce tem a tool criar_pedido, USE-A direto. Se nao tem (papel
  funcionario), diga 'Voce e funcionario, precisa pedir pro gerente' —
  NAO crie tarefa substituta.

TOOLS DISPONIVEIS — ACOES:
- criar_pedido: encomenda da LOJA pra industria entregar numa data. Cobre produtos, receitas E materias-primas (ex: queijo mussarela pra salada, lagarto cozido, saco de pao de queijo) — qualquer item do catalogo que a industria mande pra loja. **NAO confunda com receber_mp** — receber_mp eh quando a INDUSTRIA registra entrada de MP do FORNECEDOR (compra/entrada externa). Se uma loja pede MP, eh criar_pedido normal.
  **Estado dos itens (campo `estado` em cada item):**
  - `null` (default): viennoiserie sai cru congelado (loja descongela, fermenta, assa); pao/sourdough sai congelado assado (loja so descongela); fornada especial sai assada fresca.
  - `backup`: pre-fermentado congelado, assa rapido. Usuario fala "X backup" / "backup de X" / "X de backup" / "X fermentado(s) e congelado(s)" / "X pre-fermentado(s)" / "X fermentado(s) congelado(s)". So pra viennoiserie. **REGRA**: se o pedido descreve estado de processamento (qualquer mencao a "fermentado" combinado com "congelado", ou "pre-fermentado"), eh backup — popule `estado: "backup"` no item.
  - `assado`: ja assado, pronto pra vitrine. Raro — geralmente so Nebraska (forno pequeno na loja). Usuario fala "X assados".
  **Pedido misto** (ex: "5 croissants + 3 backup pra Ribeiro"): cria 2 linhas — uma com estado=null (5), outra com estado='backup' (3). NUNCA consolide ("8 croissants") — perde a distincao.
  Vocabulario: "congelado" sozinho / "cru" / sem qualificador = padrao (estado=null). "backup" / "fermentado e congelado" / "pre-fermentado" = estado='backup'. "assado" = estado='assado'.
- editar_pedido: edita pedido existente — APENAS quando status eh 'pendente' ou 'confirmado' (depois disso o estoque ja foi tocado e a edicao eh bloqueada). Use quando usuario disser "muda o pedido X pra...", "tira/aumenta/troca item do pedido X", "joga o pedido X pra outro dia", "corrige obs do pedido X". Params: `pedido_id` (obrigatorio); `data_entrega`/`observacao` opcionais (null = mantem). Se for mexer em ITENS, primeiro chame consultar_pedido pra ver a composicao atual, depois mande a lista COMPLETA em `itens` (os atuais + as mudancas) — REPLACE total. NAO muda loja nem driver — pra isso cancele e recrie. Se o usuario so disser que quer editar o pedido X sem especificar O QUE mudar, chame consultar_pedido pra mostrar a composicao e em seguida PERGUNTE objetivamente o que ele quer alterar (item / quantidade / data / observacao) — nao pare apenas exibindo o pedido.
- mudar_status_pedido: muda status de um pedido. **APENAS 3 ESTADOS** existem pro usuario:
  1. **pedido feito** — criado, aguardando producao
  2. **enviado** — saiu da industria; sistema gera QR. Motorista escaneia + PIN. *No celular dele aparece um botao 'gerar QR de entrega' — ele vai usar isso ao chegar na loja.*
  3. **recebido** — chegando na loja, motorista mostra QR de entrega no celular, alguem da loja escaneia + digita PIN da loja. Sistema finaliza como recebido e soma no estoque.

  Mapeamento → novo_status:
  * 'enviar' / 'motorista vai levar' / 'motorista chegou' / 'vai sair' / 'sair' / 'pronto pra sair' / 'separado' / 'pronto pra ir' → **novo_status='separar'** (NAO 'enviar' literal! 'separar' eh o que GERA O QR pro motorista — eh isso que o usuario chama de "enviado")
  * 'recebido' / 'entregue' / 'chegou na loja' / 'entreguei' / 'recebi' → **novo_status='receber'**
    ⚠️ ENTREGA EXIGE FOTO (regra de 13/06/2026): nao da pra fechar recebimento por aqui (texto nao anexa foto). Ao receber 'recebi/entregue/chegou', oriente o usuario a abrir a ficha do pedido no app e confirmar COM A FOTO do pedido recebido (/pedidos/<id>). Se ele mandar a foto junto na mensagem, use anexar_foto_pedido pra guardar a foto, mas o recebimento em si (somar estoque + marcar recebido) acontece no app, com a foto. A tool 'receber' vai recusar e devolver esse mesmo aviso — repasse-o com gentileza.
  * 'cancelar' / 'cancelado' → novo_status='cancelar'

  IMPORTANTE: nas respostas em texto pro usuario, use APENAS 'pedido feito' / 'enviado' / 'recebido'. NUNCA fale 'separado', 'em_transporte', 'confirmado'. Quando geramos QR de saida (apos novo_status='separar'), diga 'pedido enviado — motorista escaneia o QR abaixo'. Se usuario nao mencionar pedido_id, consulte com consultar_pedido por loja + data primeiro.
- anexar_foto_pedido: anexa foto(s) de comprovante a um pedido (ex: foto da entrega, nota fiscal). Usa as imagens da mensagem do usuario no Slack. Se o usuario mandar foto e dizer "recebi pedido X", chame **as duas** tools em sequencia OU pergunte qual fazer primeiro.
- receber_mp: registrar entrada de materia-prima (compra/fornecedor). Se a NF/nota vier em KG ou SACOS e a MP for cadastrada em 'un' (com peso por unidade — ex: pao de queijo, bolinha de 18g), passe `quantidade_kg` com o peso total (saco de 2kg x4 = quantidade_kg 8) e o sistema converte pra unidades; o peso do saco costuma estar nas observacoes da MP. So use `quantidade` quando o numero ja esta na unidade do cadastro.
- ajuste_estoque: quebra, perda, contagem fisica de MP
- criar_fornecedor: cadastrar novo fornecedor
- marcar_ponto: registrar ponto de funcionario (entrada, saida, almoco)
- criar_tarefa: criar tarefa em projetos (inbox ou projeto especifico)
- balanco_congelados: balanco/inventario do estoque de congelados (SOBRESCREVE quantidades). Use quando o usuario ditar uma contagem fisica do freezer — valores absolutos, nao deltas. Diferente de 'entrada de producao' (que soma).
- entrada_lote_loja: entrada em lote no estoque de uma LOJA especifica (SOMA quantidades). Use quando o usuario disser 'dar entrada na loja X', 'chegou entrega na loja Y', 'somar no estoque da loja Z' + lista de itens. Precisa do loja_id — se nao souber qual loja, pergunte primeiro. Itens sem cadastro entram como pendentes.
- devolver_industria: devolve SOBRAS de uma loja pra INDUSTRIA (baixa o estoque da loja E credita o congelado da industria no mesmo ato — na receita de retorno quando configurada, ex: croissants tradicionais devolvidos viram 'Croissant Tradicional — Retorno', que o Croissant Almond consome). Use APENAS quando as sobras JA CHEGARAM fisicamente na industria ('voltaram X pra industria', 'mandei as sobras de volta'). Pra agendar a coleta de amanha, use criar_retirada_sobras.
- criar_retirada_sobras: agenda a RETIRADA das sobras reaproveitaveis pra o motorista coletar AMANHA (QR code, esteira igual as entregas: coleta baixa a loja, recebimento credita a industria). **FLUXO OBRIGATORIO pos-sobras**: quando o resultado de registrar_desperdicio/lote vier com `retirada_sugerida`, PERGUNTE ao usuario 'da sobra de <item>, quantos voltam pra industria pra virar <destino>?'. Com a resposta, PECA A FOTO da sobra ('me manda uma foto da sobra pra eu criar a retirada'). Quando a mensagem com a foto chegar, chame esta tool (a foto e OBRIGATORIA — sem imagem anexada na mensagem a tool recusa). Os itens que NAO voltam (viram nutella na loja) nao precisam de nada.
- registrar_desperdicio: baixa do estoque da loja com motivo. 6 motivos:
  * **validade** (default): item venceu. Sinonimos: 'venceu', 'vencido', 'passou da validade'.
  * **nao_vendeu**: sobra do dia que NAO foi vendida. Sinonimos: 'sobrou', 'sobra', 'nao vendeu', 'restou'.
  * **estragou**: irreaproveitavel. Sinonimos: 'estragado', 'mofou', 'azedou'.
  * **caiu**: item caiu no chao.
  * **queimou**: queimado no forno. Sinonimo: 'queimado'.
  * **outro**: qualquer outro motivo.
  **REGRA REAPROVEITAVEL**: alguns itens tem flag `reaproveitavel=true` (cadastrada pelo admin) — quando o motivo eh 'validade' OU 'nao_vendeu', o desperdicio eh REGISTRADO mas o estoque NAO eh baixado (item vai virar outra coisa). O servidor decide automaticamente — o copilot so passa o motivo certo.
  Use quando o usuario disser 'venceu X', 'descartei Y', 'sobrou no balcao'. **USE APENAS PARA 1 ITEM.** Para LISTA (2+ itens), use registrar_desperdicio_lote SEMPRE. **SEMPRE preencha `loja_nome` com o que o usuario falou** (ex: 'nebraska', 'anesio'). O servidor faz fuzzy match com a lista de lojas. Se o usuario NAO mencionou loja e o user logado nao tem loja padrao, pergunte qual loja antes de chamar a tool.
- registrar_desperdicio_lote: REGRA CRITICA — quando o usuario passar uma LISTA de itens vencidos/descartados ('anota essas sobras', '2 croissants vencidos, 3 pao frances, 1 nutella...'), CHAME ESTA TOOL UMA VEZ SO com todos os itens no array `itens`. NUNCA chame registrar_desperdicio multiplas vezes — o sistema ignora chamadas em paralelo. Motivo unico pro lote (default vencido). Mesma regra de loja: SEMPRE preencha loja_nome; se nao tiver, pergunte. **NUNCA re-envie itens ja registrados**: se um lote acabou de ser confirmado e o usuario acrescentar um item que faltou, chame a tool de novo APENAS com o item novo — repetir a lista inteira DUPLICA as perdas (aconteceu em 02/07/2026).
- criar_venda_b2b: venda da INDUSTRIA pra cliente externo (hotel, restaurante). Baixa do FREEZER (EstoqueProducao), NAO de loja. B2B NAO TEM LOJA — nunca pergunte "qual loja". Use quando o usuario disser 'vendi pro hotel X', 'fatura essa venda pro restaurante Y'. Cliente_nome: passe como o usuario disse — servidor faz fuzzy match. Se nao achar, fica como avulso (ok); NAO fique consultando o cliente em loop, so crie. **data_entrega e OBRIGATORIA** (dia que vai pro padeiro produzir/separar) — se o usuario nao disser, pergunte "pra que dia e a entrega?". ESTADO do item: se o usuario disser "Croissant backup"/"Cinnamon Roll backup", o nome e a receita ("Croissant Tradicional", "Cinnamon Roll") e o estado e "backup" (idem "assado"); default cru. Preços: NAO preencha preco_unitario a nao ser que o usuario explicite — servidor pega do cadastro (Receita.preco_venda / Produto.preco_atacado) + desconto do cliente. Parcelas: omita se for a vista, ou liste {{vencimento, valor, forma_pagamento}}.
- criar_cliente_b2b: cadastra cliente novo. Use quando o usuario disser 'cadastra cliente X' ou se uma venda B2B falhar por cliente inexistente e o usuario confirmar criar. Nome obrigatorio; demais campos opcionais (telefone, cnpj, contato, desconto%).
- consultar_cliente_b2b: ver clientes cadastrados. Use antes de criar venda pra grandes redes (saber desconto, contato).

TOOLS DISPONIVEIS — CONSULTAS (read, sem aprovacao):
- consultar_pedido: ver pedidos por loja/data/status/id
- consultar_estoque: escopos `producao` (freezer/industria), `loja` (uma loja), `todos` (producao + todas as lojas), `mp` (materias-primas). **DEFAULT = `todos`**: use sempre que o usuario perguntar "estoque", "quanto temos", "estoque de X", "estoque em todas as lojas". So use `loja` quando ele citar UMA loja especifica (ex: "estoque da Anesio"). So use `mp` quando ele falar explicitamente "materia-prima" ou "MP". So use `producao` quando ele falar especificamente "industria", "freezer", "congelados". Em duvida → `todos`.
- consultar_fornecedores: lista fornecedores
- consultar_margem: custo + preco + margem de receita/produto
- consultar_funcionario: info de funcionario
- consultar_caixa: numeros do dia (entregas, pedidos locais, compras MP)
- consultar_vendas_itens: vendas POR PRODUTO no PDV/Seru no intervalo (top N + filtro de loja). Use pra 'o que mais vendeu', 'top produtos', 'quanto saiu de X'.
- consultar_desperdicio: lista desperdicios (sobra do dia) por periodo + loja. Use pra 'quanto venceu', 'desperdicio da semana', 'sobrou de X'.
- consultar_foco: lista projetos foco_12s + tarefas pendentes deles
- consultar_tarefas: lista tarefas com filtros (atrasadas, pendentes, projeto, foco)
- consultar_vigia (owner): O QUE A VIGIA DO CHATBOT DO SITE FLAGROU. Cada alerta que chega no WhatsApp do dono (reclamacao, handoff preguicoso, 'bot delirou', cliente esperando humano) mora aqui. Use pra 'qual foi a reclamacao', 'me da detalhes do alerta', 'o que aconteceu na conversa X' (passe conv_id), 'mostra os vereditos de hoje'. Tem campo `palavra` pra filtrar (ex: 'reclamacao', 'cesta', 'esperando').
- consultar_conversa_chatwoot (owner): mensagens REAIS de uma conversa do Chatwoot. Use depois do consultar_vigia pra ver o dialogo cliente↔bot↔atendente que disparou o alerta.
- listar_conversas_chatwoot (owner): quem ta na fila — 'pending' (aguardando bot) ou 'open' (cliente esperando atendente humano). Use pra 'tem alguem esperando', 'quem ta na fila'.

TOOLS DISPONIVEIS — MEMORIA PERSISTENTE (notas markdown — usa SEMPRE):
- consultar_notas(termo?): busca regras/excecoes de negocio que voce ou outros usuarios CADASTRARAM em sessoes anteriores (ex: "loja X nao vende Y", "fornecedor Z atrasa sexta"). NAO ABUSE: como o sistema executa SO 1 tool por turno (sem loop), consultar_notas come o turno e deixa o usuario sem resposta. USE consultar_notas APENAS quando: (a) o usuario PERGUNTAR sobre regra/decisao ("o que combinamos sobre X"), OU (b) voce vai EXECUTAR uma write e desconfia que pode existir uma excecao do banco (ex: criar_pedido pra uma loja que talvez nao venda aquele item — nesse caso consulte com termo). NUNCA chame consultar_notas como passo automatico de "inicio de conversa" quando o usuario ja mandou uma ORDEM DIRETA E COMPLETA (lista de itens, criar pedido, registrar venda, etc) — vai direto pra tool de acao; se faltar contexto, a write tem preview pra confirmar. Termo vazio = ultimas notas.
- registrar_nota(titulo, conteudo, tags?): grava REGRA/EXCECAO/DECISAO que precisa lembrar nas proximas conversas. USE quando o usuario te ENSINAR algo explicitamente ("a partir de hoje X", "agora vai ser assim", "guarda essa regra", "lembra disso"). NAO use pra dados efemeros (pedido especifico, lembrete do dia). Confirme no texto o que registrou — usuario corrige se entendeu errado.

TOOLS DISPONIVEIS — PLANEJAMENTO (PARA + 12 Week Year):
- marcar_tarefa_feita: marca uma tarefa como concluida (preview)
- criar_tarefa: cria nova tarefa em projeto ou inbox

REGRAS:
- PREFIRA RESPONDER A PERGUNTAR (14/06/2026, Opus 4.8): se voce tem como
  inferir/escolher com confianca razoavel, RESPONDA e siga — nao pare pra
  pedir esclarecimento de cada campo. Use o catalogo, o historico, o
  contexto e escolha o mais provavel; mencione no texto qual escolheu pra
  o usuario corrigir DEPOIS se quiser. So pergunte quando: (a) a tool eh
  WRITE de dinheiro/estoque e a ambiguidade tem custo real (ex: loja
  errada move estoque errado — nesse caso a REGRA DA LOJA abaixo manda
  perguntar mesmo), (b) o dado eh impossivel de inferir sem chutar (ex:
  ID que nao apareceu em lugar nenhum), (c) duas interpretacoes mudam
  qual tool chamar. Em tudo mais, decida e siga.
- SINTETIZE — NUNCA ECOE A TOOL (19/06/2026): quando o usuario pedir
  PREVISAO, MEDIA, TENDENCIA, COMPARACAO, RANKING, ou qualquer pergunta
  ANALITICA ("o que prever pra semana que vem", "qual item mais sai",
  "quanto subiu vs mes passado", "media diaria de X"), as tools de READ
  servem pra te DAR OS DADOS. Voce faz a CONTA com os dados que vieram e
  responde com o RESULTADO — nao copie a lista crua do retorno da tool
  pro usuario. Caso real (19/06/2026 — dono pediu "previsao da semana que
  vem da Anesio considerando 2 semanas"): o bot devolveu 21 pedidos em
  texto cru em vez de somar quantidades, dividir por 2 e mostrar o pedido
  previsto (reincidiu em 23/06/2026 com "as 3 lojas").
  PREVISAO DE PEDIDO DE REPOSICAO ("quanto pedir pra semana que vem",
  "previsao das lojas", "as 3 lojas", "baseado nas ultimas N semanas"):
  USE A TOOL `prever_pedido` (semanas=N; `loja` opcional — vazio = todas as
  lojas). Ela JA soma por item e divide pelas semanas, server-side, e devolve
  o `sugerido` por loja. NAO faca essa conta na mao com consultar_pedido —
  foi exatamente isso que falhou (modelo despejou o historico). Apresente a
  lista sugerida por loja + mencione a base ("3 pedidos em 3 semanas — base
  curta" quando for pouca). Pra OUTRAS analises (media/ranking/tendencia que
  nao sejam previsao de pedido), as tools de read te dao os dados e VOCE faz
  a conta — entregue o RESULTADO, nao a lista crua.
  FOLLOW-UP que so muda a JANELA ("agora 4 semanas", "e de 3 meses?",
  "considera so esse mes"): eh a MESMA analise da mensagem anterior — mantenha
  a loja e o item do contexto, mude so o periodo, e RE-SINTETIZE (divida pelo
  novo numero de semanas). Nunca volte a despejar a lista crua.
  NAO SE IMITE: se em turnos ANTERIORES a resposta saiu como lista crua de
  pedidos (despejo), NAO repita esse formato — aquilo foi erro. Toda pergunta
  analitica responde com o RESULTADO calculado, mesmo que o historico mostre
  voce fazendo diferente antes.
- Use o nome EXATO dos catalogos. Se ambiguo ('100 croissants' com varios tipos),
  escolha o mais provavel e mencione na sua resposta-texto que o usuario confirme.
- Datas relativas: resolva 'amanha', 'sexta', 'segunda', etc. pra YYYY-MM-DD.
- REGRA DA LOJA — CRITICA: NUNCA assuma uma loja "default" do usuario.
  Cada usuario pode atuar em qualquer loja, nao tem responsavel fixo. Se o
  usuario nao mencionou a loja em criar_pedido / registrar_desperdicio,
  **NAO chame a tool**: pergunte primeiro "Pra qual loja?" e mostre as
  opcoes do catalogo. So chame a tool quando o usuario falar o nome da
  loja explicitamente — e nesse caso sempre preencha o campo loja_nome.
- BACKUP — REGRA: em criar_pedido, quando o usuario disser 'X de backup',
  'X backup' ou 'backup de X', isso significa um item ultra-congelado JA
  RECHEADO pra reposicao rapida. Preencha observacao='backup' no item
  correspondente. Se ele pedir '5 croissants e mais 3 backup', envie 2
  itens separados na lista: primeiro item com nome='Croissant' quantidade=5
  sem observacao, segundo item com nome='Croissant' quantidade=3
  observacao='backup'. Backup e uma variacao do mesmo produto — NAO procure
  'Croissant Backup' no catalogo, use o nome base.
- Pra mudar_status_pedido, se o usuario nao mencionar id explicitamente
  ('marca o pedido de hoje como entregue'), CONSULTE primeiro com
  consultar_pedido pra achar e pergunte 'quer mudar o status do pedido #X?'.
- marcar_ponto: se nao especificar tipo, assuma 'entrada' se for cedo (<13h)
  ou 'saida' senao. Sempre mencione no texto qual escolheu.
- DESPERDICIO LISTA — REGRA CRITICA: quando o usuario ditar uma LISTA de
  itens vencidos/descartados/sobras (ex: '2 croissants, 3 pao frances, 1
  nutella vencidos'; 'anota essas sobras: ...'; 'descartei isso aqui:
  ...'), CHAME registrar_desperdicio_lote UMA VEZ SO com TODOS os itens
  no array `itens`. NUNCA chame registrar_desperdicio multiplas vezes
  em paralelo — fica errado. Use registrar_desperdicio APENAS quando
  for 1 item unico ('venceu 2 croissants').
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


def interpretar(prompt_text, user, historico=None, images=None,
                apenas_leitura=False, modelo=None, system_extra=None,
                tools_whitelist=None):
    """Chama Claude. Retorna dict com tipo, params, explicacao.

    historico: lista de {role: 'user'|'assistant', content: str} com
    conversas anteriores nessa sessao. Permite copilot lembrar contexto
    ('ah entendi, foi aqui' depois de uma resposta).

    images: lista opcional de {mimetype, base64} pra mandar imagens junto
    com o prompt (vision do Haiku). Usado pelo Slack bot.

    apenas_leitura: se True, remove TODAS as tools de write (REQUER_APROVACAO)
    antes de mandar pra Claude. Usado pelo bot WhatsApp do dono (modo so
    consulta). Como Claude nem ve a tool, nao tem como tentar usa-la.

    modelo: override do model id (default MODELO_DEFAULT/Sonnet). O bot do
    WhatsApp do dono passa Opus — fork de modelo por canal, motor unico.

    system_extra: bloco de persona por canal, anexado ao fim do system
    prompt (ex: persona de assessor do dono no WhatsApp).

    tools_whitelist: se setado (set/lista de nomes), restringe as tools
    visiveis APENAS a essas (alem do filtro de papel). Usado pelo Slack em
    modo "so desperdicio" quando o bot de pedidos esta desativado — Claude nem
    ve as outras tools.
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY') or current_app.config.get('ANTHROPIC_API_KEY')
    if not api_key:
        return {'tipo': 'erro', 'explicacao': 'Copilot indisponivel: ANTHROPIC_API_KEY nao configurada.', 'raw': None}
    try:
        import anthropic
    except ImportError:
        return {'tipo': 'erro', 'explicacao': 'Biblioteca anthropic nao instalada.', 'raw': None}

    client = anthropic.Anthropic(api_key=api_key)
    # System prompt construido DEPOIS do filtro de tools (mais abaixo) —
    # placeholder aqui; ver bloco tools_filtradas.
    system = None

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
    if apenas_leitura:
        tools_filtradas = [t for t in tools_filtradas
                           if t.get('name') not in REQUER_APROVACAO]
    if tools_whitelist is not None:
        tools_filtradas = [t for t in tools_filtradas
                           if t.get('name') in tools_whitelist]
    if not tools_filtradas:
        return {'tipo': 'erro', 'explicacao': 'Sem permissao pra usar o copilot.', 'raw': None}
    # Prompt lista exatamente as tools enviadas — nunca promete write que o
    # filtro removeu (ver _build_system_prompt).
    system = _build_system_prompt(user, tools_visiveis=tools_filtradas,
                                  apenas_leitura=apenas_leitura)
    if system_extra:
        system = system + '\n\n' + system_extra.strip()

    # Cache breakpoint na ULTIMA tool: marca todo o bloco de tools (schema
    # gigante, ~5-10KB) como cacheable. Junto com o cache do system_prompt,
    # cobre ~95% dos tokens de input em conversas comuns — custo cai ~90%
    # depois do primeiro request da janela de 5min.
    tools_com_cache = [dict(t) for t in tools_filtradas]
    if tools_com_cache:
        tools_com_cache[-1] = {**tools_com_cache[-1],
                                'cache_control': {'type': 'ephemeral'}}

    try:
        response = client.messages.create(
            model=modelo or MODELO_DEFAULT,
            max_tokens=4000,
            system=[{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}],
            tools=tools_com_cache,
            messages=messages,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception('Copilot: erro Anthropic')
        return {'tipo': 'erro', 'explicacao': f'Erro Anthropic: {exc}', 'raw': None}

    tool_calls_raw = []
    texto_partes = []
    for block in response.content:
        if block.type == 'tool_use':
            tool_calls_raw.append({'name': block.name, 'input': block.input})
        elif block.type == 'text':
            texto_partes.append(block.text)

    # Consolidacao defensiva: se Claude chamar `registrar_desperdicio` mais de
    # uma vez na mesma resposta (que era o bug antigo — handler so pegava o
    # ultimo e os outros silenciosamente sumiam), merge em UMA chamada de
    # `registrar_desperdicio_lote`. Mesma loja e motivo do primeiro.
    desp_calls = [tc for tc in tool_calls_raw if tc['name'] == 'registrar_desperdicio']
    if len(desp_calls) >= 2:
        primeiro = desp_calls[0]['input']
        itens_merged = []
        for tc in desp_calls:
            inp = tc.get('input') or {}
            itens_merged.append({
                'nome': inp.get('item_nome') or '',
                'quantidade': inp.get('quantidade') or 0,
                'observacao': inp.get('observacao'),
            })
        consolidado = {
            'loja_id': primeiro.get('loja_id'),
            'loja_nome': primeiro.get('loja_nome'),
            'motivo': primeiro.get('motivo') or 'vencido',
            'itens': itens_merged,
        }
        tool_name = 'registrar_desperdicio_lote'
        tool_call = consolidado
    elif tool_calls_raw:
        # Caso normal: pega o ultimo (comportamento original)
        ultima = tool_calls_raw[-1]
        tool_name = ultima['name']
        tool_call = ultima['input']
    else:
        tool_name = None
        tool_call = None

    explicacao = ' '.join(texto_partes).strip() or '(sem comentario do copilot)'
    raw = {
        'stop_reason': response.stop_reason,
        'usage': {
            'input': response.usage.input_tokens, 'output': response.usage.output_tokens,
            'cache_read': getattr(response.usage, 'cache_read_input_tokens', 0),
            'cache_create': getattr(response.usage, 'cache_creation_input_tokens', 0),
        },
    }

    # Registro de custo: separa copilot do Slack (Sonnet) do WhatsApp do dono
    # (Opus) — mesmo motor, canais e modelos distintos.
    _canal_uso = 'whatsapp' if apenas_leitura else 'slack'
    from app.services import uso_ia
    uso_ia.registrar(f'copilot_{_canal_uso}', modelo or MODELO_DEFAULT,
                     getattr(response, 'usage', None), canal=_canal_uso)

    if tool_call and tool_name:
        # Enriquece params com info do banco (matches de produto/MP)
        params = _enriquecer_params(tool_name, tool_call, user)
        # Dica de canal pros handlers (origem da nota etc): apenas_leitura
        # = True hoje só vem do WhatsApp do dono (zapi_bot).
        params['_canal'] = 'whatsapp' if apenas_leitura else 'slack'
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
    if tool_name == 'editar_pedido':
        return _enriquecer_editar_pedido(tool_input)
    if tool_name in ('receber_mp', 'ajuste_estoque'):
        nome = (tool_input.get('mp_nome') or '').strip()
        matches = _resolver_mp(nome) if nome else []
        out = {**tool_input, 'mp_matches': matches,
               'mp_resolvida': matches[0] if matches else None}
        if tool_name == 'receber_mp':
            qtd_final, rotulo, erro = _quantidade_recebimento_mp(out)
            out['quantidade_convertida'] = qtd_final
            out['conversao_rotulo'] = rotulo
            out['conversao_erro'] = erro
        return out
    if tool_name == 'balanco_congelados':
        return _enriquecer_balanco_congelados(tool_input)
    if tool_name == 'entrada_lote_loja':
        return _enriquecer_entrada_lote_loja(tool_input)
    if tool_name == 'devolver_industria':
        return _enriquecer_devolver_industria(tool_input, user)
    if tool_name == 'criar_retirada_sobras':
        return _enriquecer_criar_retirada_sobras(tool_input, user)
    if tool_name == 'registrar_desperdicio':
        return _enriquecer_registrar_desperdicio(tool_input, user)
    if tool_name == 'registrar_desperdicio_lote':
        return _enriquecer_registrar_desperdicio_lote(tool_input, user)
    if tool_name == 'criar_venda_b2b':
        return _enriquecer_criar_venda_b2b(tool_input)
    if tool_name == 'criar_cliente_b2b':
        return tool_input  # nada pra resolver
    # consultar_pedido / consultar_estoque: passam direto
    return tool_input


def _enriquecer_criar_venda_b2b(tool_input):
    """Resolve cliente (fuzzy match) + cada item + preco sugerido + saldo
    do freezer pro preview. Reusa _resolver_produto + preco_sugerido."""
    from sqlalchemy import func

    from app.models import ClienteB2B, EstoqueProducao
    from app.services.vendas_b2b import preco_sugerido

    out = dict(tool_input)
    nome_cli = (out.get('cliente_nome') or '').strip()
    cliente = None
    if nome_cli:
        cliente = (ClienteB2B.query
                   .filter(func.lower(ClienteB2B.nome) == nome_cli.lower(),
                           ClienteB2B.ativo.is_(True))
                   .first())
        if not cliente:
            cliente = (ClienteB2B.query
                       .filter(ClienteB2B.nome.ilike(f'%{nome_cli}%'),
                               ClienteB2B.ativo.is_(True))
                       .first())
    out['cliente_id'] = cliente.id if cliente else None
    out['cliente_nome_resolvido'] = cliente.nome if cliente else nome_cli
    out['cliente_desconto'] = cliente.desconto_percentual if cliente else 0
    out['cliente_avulso'] = cliente is None

    # Itens
    itens_enriq = []
    total = 0.0
    pend_b2b = None   # comprometido B2B pendente — carregado 1x, no 1º uso
    for it in (out.get('itens') or []):
        nome_in = (it.get('nome') or '').strip()
        # "Croissant backup" -> resolve "Croissant" + estado=backup. Estado
        # explicito do Claude tem prioridade sobre o que veio no nome.
        nome, est_nome = _separar_estado(nome_in)
        est = (str(it.get('estado') or '').strip().lower() or None) or est_nome
        if est not in (None, 'backup', 'assado'):
            est = None
        try:
            qtd = int(it.get('quantidade') or 0)
        except (TypeError, ValueError):
            qtd = 0
        if not nome or qtd <= 0:
            itens_enriq.append({'nome': nome_in or '?', 'quantidade': qtd,
                                 'erro': 'invalido'})
            continue
        matches = _resolver_produto(nome)
        resolvido = matches[0] if matches else None

        preco_unit = it.get('preco_unitario')
        if preco_unit is None and resolvido:
            preco_unit = preco_sugerido(
                receita_id=resolvido['id'] if resolvido['tipo'] == 'receita' else None,
                produto_id=resolvido['id'] if resolvido['tipo'] == 'produto' else None,
                cliente=cliente,
            )
        if preco_unit is None:
            preco_unit = 0
        desc = float(it.get('desconto_percentual') or 0)
        subtotal = qtd * preco_unit * (1 - desc / 100.0)
        total += subtotal

        # Saldo DISPONIVEL no freezer pra UI mostrar: fisico menos o
        # comprometido com vendas B2B ainda nao separadas (a baixa e na
        # separacao, 07/07/2026) — sem o desconto, duas vendas podiam ser
        # aprovadas contra o mesmo saldo.
        estoque_atual = None
        if resolvido:
            if pend_b2b is None:
                from app.services.vendas_b2b import comprometido_b2b_pendente
                pend_b2b = comprometido_b2b_pendente()
            ep = EstoqueProducao.query.filter_by(
                receita_id=resolvido['id'] if resolvido['tipo'] == 'receita' else None,
                produto_id=resolvido['id'] if resolvido['tipo'] == 'produto' else None,
            ).first()
            pend = pend_b2b.get((resolvido['tipo'], resolvido['id']), 0)
            estoque_atual = (ep.quantidade if ep else 0) - pend

        itens_enriq.append({
            'nome_original': nome_in,
            'quantidade': qtd,
            'matches': matches,
            'resolvido': resolvido,
            'estado': est,
            'preco_unitario': round(float(preco_unit), 2),
            'desconto_percentual': desc,
            'subtotal': round(subtotal, 2),
            'estoque_atual': estoque_atual,
        })

    try:
        frete = float(out.get('frete_valor') or 0)
    except (TypeError, ValueError):
        frete = 0.0
    return {
        'cliente_nome': out.get('cliente_nome'),
        'cliente_id': out.get('cliente_id'),
        'cliente_nome_resolvido': out['cliente_nome_resolvido'],
        'cliente_desconto': out['cliente_desconto'],
        'cliente_avulso': out['cliente_avulso'],
        'data_venda': out.get('data_venda'),
        'data_entrega': out.get('data_entrega'),
        'nf_numero': out.get('nf_numero'),
        'observacao': out.get('observacao'),
        'itens': itens_enriq,
        'parcelas': out.get('parcelas') or [],
        'frete_valor': round(frete, 2),
        # total do preview = itens + frete (mesma conta da venda persistida)
        'total': round(total + frete, 2),
    }


def _retirada_sugerida_preview(tipo_item, item_id, nome_ok, qtd, motivo):
    """Sugestao de retirada calculada JA NO PREVIEW, nao so na execucao —
    o combinado do dono (02/07/2026) e o bot perguntar "quantos voltam pra
    industria?" NA HORA em que a sobra e falada, nao depois do botao.
    Devolve o dict da sugestao ou None (item sem retorno configurado,
    motivo nao-reaproveitavel, MP/produto)."""
    from app.services.desperdicio_core import reaproveita_sem_baixa
    if tipo_item != 'receita' or not item_id:
        return None
    if not reaproveita_sem_baixa('receita', item_id, motivo):
        return None
    rec = db.session.get(Receita, item_id)
    if rec is None or not rec.retorno_receita_id:
        return None
    return {'item': nome_ok, 'qtd_sobra': qtd,
            'destino': (rec.retorno_receita.nome
                        if rec.retorno_receita else nome_ok)}


def _enriquecer_registrar_desperdicio(tool_input, user):
    """Resolve loja_nome + item_nome no banco antes do preview. Marca
    `retiradas_sugeridas` quando a sobra pode voltar pra industria."""
    from app.services.desperdicio_core import normalizar_motivo
    from app.utils import resolver_loja_por_nome
    out = dict(tool_input)
    loja = None
    try:
        loja_id = int(out.get('loja_id') or 0) or None
    except (TypeError, ValueError):
        loja_id = None
    if loja_id:
        loja = Loja.query.get(loja_id)
    if not loja:
        loja = resolver_loja_por_nome(out.get('loja_nome'))
    if loja:
        out['loja_id'] = loja.id
        out['loja_nome'] = loja.nome

    motivo = normalizar_motivo(out.get('motivo'))
    resolvido = _resolver_item_qualquer((out.get('item_nome') or '').strip())
    try:
        qtd = int(out.get('quantidade') or 0)
    except (TypeError, ValueError):
        qtd = 0
    if resolvido and qtd > 0:
        tipo_item, item_id, nome_ok = resolvido
        s = _retirada_sugerida_preview(tipo_item, item_id, nome_ok, qtd,
                                       motivo)
        if s:
            out['retiradas_sugeridas'] = [s]
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


def _enriquecer_devolver_industria(tool_input, user):
    """Resolve loja + itens + estoque atual + DESTINO do retorno pro preview.

    O destino mostra pra onde o credito vai na industria (receita de retorno
    configurada na ficha, ex: 'Croissant Tradicional — Retorno'; sem config,
    a propria receita). MP nao tem estoque na industria — vira erro no item."""
    from app.models import EstoqueLoja, Receita
    from app.utils import resolver_loja_por_nome

    out = dict(tool_input)
    loja = None
    try:
        loja_id = int(out.get('loja_id') or 0) or None
    except (TypeError, ValueError):
        loja_id = None
    if loja_id:
        loja = Loja.query.get(loja_id)
    if not loja:
        loja = resolver_loja_por_nome(out.get('loja_nome'))
    out['loja_id'] = loja.id if loja else None
    out['loja_nome'] = loja.nome if loja else out.get('loja_nome')

    itens_enriq = []
    for it in (out.get('itens') or []):
        nome = (it.get('nome') or '').strip()
        try:
            qtd = int(it.get('quantidade') or 0)
        except (TypeError, ValueError):
            qtd = 0
        if not nome or qtd <= 0:
            itens_enriq.append({'nome': nome or '?', 'quantidade': qtd,
                                'erro': 'invalido'})
            continue
        resolvido = _resolver_item_qualquer(nome)
        if not resolvido:
            itens_enriq.append({'nome': nome, 'quantidade': qtd,
                                'resolvido': None, 'estoque_atual': None})
            continue
        tipo_item, item_id, nome_ok = resolvido
        if tipo_item == 'mp':
            itens_enriq.append({'nome': nome, 'quantidade': qtd,
                                'resolvido': {'tipo': tipo_item, 'id': item_id,
                                              'nome': nome_ok},
                                'erro': 'MP nao pode ser devolvida a industria'})
            continue
        estoque_atual = None
        if out['loja_id']:
            filtro = {'loja_id': out['loja_id'],
                      'receita_id': item_id if tipo_item == 'receita' else None,
                      'produto_id': item_id if tipo_item == 'produto' else None,
                      'materia_prima_id': None}
            el = EstoqueLoja.query.filter_by(**filtro).first()
            estoque_atual = el.quantidade if el else 0
        destino = nome_ok
        if tipo_item == 'receita':
            rec = Receita.query.get(item_id)
            if rec and rec.retorno_receita_id and rec.retorno_receita:
                destino = rec.retorno_receita.nome
        itens_enriq.append({
            'nome': nome, 'quantidade': qtd,
            'resolvido': {'tipo': tipo_item, 'id': item_id, 'nome': nome_ok},
            'estoque_atual': estoque_atual,
            'destino_industria': destino,
        })

    n_ok = sum(1 for i in itens_enriq
               if i.get('resolvido') and not i.get('erro'))
    out['itens'] = itens_enriq
    out['totais'] = {
        'total_itens': len(itens_enriq),
        'resolvidos': n_ok,
        'nao_resolvidos': len(itens_enriq) - n_ok,
    }
    return out


def _enriquecer_criar_retirada_sobras(tool_input, user):
    """Resolve loja + itens + destino do retorno pro preview da retirada.
    Reusa o enricher da devolucao (mesma forma de itens/destino)."""
    out = _enriquecer_devolver_industria(tool_input, user)
    from datetime import timedelta as _td

    from app.utils import hoje
    out['data_retirada'] = (hoje() + _td(days=1)).isoformat()
    return out


def _enriquecer_registrar_desperdicio_lote(tool_input, user):
    """Resolve loja + cada item + estoque atual pra preview de lote. Tambem
    marca `ja_registrado_hoje` por item — o preview avisa quando o lote
    parece duplicado (caso real 02/07/2026: o modelo re-enviou a lista
    inteira pra acrescentar 1 item e 4 itens duplicaram como perda)."""

    from app.constants import DESPERDICIO_MOTIVOS
    from app.models import EstoqueLoja
    from app.utils import resolver_loja_por_nome

    out = dict(tool_input)
    loja = None
    try:
        loja_id = int(out.get('loja_id') or 0) or None
    except (TypeError, ValueError):
        loja_id = None
    if loja_id:
        loja = Loja.query.get(loja_id)
    if not loja:
        loja = resolver_loja_por_nome(out.get('loja_nome'))
    loja_id = loja.id if loja else None
    loja_nome = loja.nome if loja else None

    # MESMA normalizacao do executor (executar_registrar_desperdicio_lote) —
    # antes este preview usava o vocabulario antigo ('vencido', 'estragado')
    # e 'nao_vendeu' virava 'vencido' silenciosamente no registro.
    motivo = (out.get('motivo') or 'validade').strip().lower()
    motivo = {'vencido': 'validade', 'estragado': 'estragou',
              'queimado': 'queimou'}.get(motivo, motivo)
    if motivo not in DESPERDICIO_MOTIVOS:
        motivo = 'validade'

    itens_enriq = []
    retiradas_sugeridas = []
    for it in (out.get('itens') or []):
        nome = (it.get('nome') or '').strip()
        try:
            qtd = int(it.get('quantidade') or 0)
        except (TypeError, ValueError):
            qtd = 0
        if not nome or qtd <= 0:
            itens_enriq.append({'nome': nome or '?', 'quantidade': qtd,
                                 'erro': 'invalido'})
            continue
        resolvido = _resolver_item_qualquer(nome)
        if not resolvido:
            itens_enriq.append({'nome': nome, 'quantidade': qtd,
                                 'observacao': (it.get('observacao') or '').strip() or None,
                                 'resolvido': None,
                                 'estoque_atual': None})
            continue
        tipo_item, item_id, nome_ok = resolvido
        estoque_atual = None
        ja_hoje = 0
        if loja_id:
            filtro = {'loja_id': loja_id}
            if tipo_item == 'receita':
                filtro['receita_id'] = item_id
            elif tipo_item == 'produto':
                filtro['produto_id'] = item_id
            else:
                filtro['materia_prima_id'] = item_id
            el = EstoqueLoja.query.filter_by(**filtro).first()
            estoque_atual = el.quantidade if el else 0
            # Quanto DESTE item ja foi registrado como desperdicio HOJE
            # nesta loja — sinal de lote duplicado no preview.
            from sqlalchemy import func as _func

            from app.models import Desperdicio
            fk_col = {'receita': Desperdicio.receita_id,
                      'produto': Desperdicio.produto_id,
                      'mp': Desperdicio.materia_prima_id}[tipo_item]
            ja_hoje = int(db.session.query(
                _func.coalesce(_func.sum(Desperdicio.quantidade), 0))
                .filter(Desperdicio.loja_id == loja_id,
                        Desperdicio.data == hoje(),
                        fk_col == item_id).scalar() or 0)
        itens_enriq.append({
            'nome': nome,
            'quantidade': qtd,
            'observacao': (it.get('observacao') or '').strip() or None,
            'resolvido': {'tipo': tipo_item, 'id': item_id, 'nome': nome_ok},
            'estoque_atual': estoque_atual,
            'ja_registrado_hoje': ja_hoje,
        })
        s = _retirada_sugerida_preview(tipo_item, item_id, nome_ok, qtd,
                                       motivo)
        if s:
            retiradas_sugeridas.append(s)

    n_ok = sum(1 for i in itens_enriq if i.get('resolvido'))
    n_nao = sum(1 for i in itens_enriq if not i.get('erro') and not i.get('resolvido'))
    n_err = sum(1 for i in itens_enriq if i.get('erro'))
    total_qtd = sum(int(i.get('quantidade') or 0) for i in itens_enriq if not i.get('erro'))
    return {
        'loja_id': loja_id,
        'loja_nome': loja_nome,
        'motivo': motivo,
        'observacao': (out.get('observacao') or '').strip() or None,
        'itens': itens_enriq,
        'retiradas_sugeridas': retiradas_sugeridas,
        'totais': {
            'total_itens': len(itens_enriq),
            'resolvidos': n_ok,
            'nao_resolvidos': n_nao,
            'erros': n_err,
            'delta_total': total_qtd,
        },
    }


def _enriquecer_criar_pedido(tool_input):
    itens_enriq = []
    for item in (tool_input.get('itens') or []):
        nome = (item.get('nome') or '').strip()
        qtd = int(item.get('quantidade') or 0)
        if not nome or qtd <= 0:
            continue
        matches = _resolver_item_pedido(nome)
        obs_item = (item.get('observacao') or '').strip() or None
        estado_item = (item.get('estado') or '').strip().lower() or None
        if estado_item not in (None, 'backup', 'assado'):
            estado_item = None
        itens_enriq.append({
            'nome_original': nome, 'quantidade': qtd,
            'observacao': obs_item, 'estado': estado_item,
            'matches': matches, 'resolvido': matches[0] if matches else None,
        })

    # Resolve loja por id OU nome (fuzzy). Preview mostra nome real ou "?".
    loja_id = tool_input.get('loja_id')
    loja_nome_input = (tool_input.get('loja_nome') or '').strip()
    loja_nome = None
    if loja_id:
        l = Loja.query.get(loja_id)
        if l:
            loja_nome = l.nome
        else:
            loja_id = None
    if not loja_id and loja_nome_input:
        from app.utils import resolver_loja_por_nome
        l = resolver_loja_por_nome(loja_nome_input)
        if l:
            loja_id = l.id
            loja_nome = l.nome
    # Se ja ha pedido aberto da loja nessa data, o preview avisa que vai juntar
    # nele (o executor reconfirma na hora de salvar).
    merge_pedido_id = None
    if loja_id and tool_input.get('data_entrega'):
        try:
            _d = datetime.strptime(tool_input['data_entrega'], '%Y-%m-%d').date()
            from app.services.pedido_merge import pedido_aberto_para_merge
            _alvo = pedido_aberto_para_merge(loja_id, _d, 'confirmado')
            merge_pedido_id = _alvo.id if _alvo else None
        except (ValueError, TypeError):
            merge_pedido_id = None
    return {
        'loja_id': loja_id, 'loja_nome': loja_nome,
        'data_entrega': tool_input.get('data_entrega'),
        'itens': itens_enriq, 'observacao': tool_input.get('observacao'),
        'merge_pedido_id': merge_pedido_id,
    }


def _enriquecer_editar_pedido(tool_input):
    """Adiciona ao tool_input: snapshot do pedido atual + itens resolvidos
    (se vieram novos) pra preview montar o diff."""
    from app.models import PedidoLoja
    pid = tool_input.get('pedido_id')
    pedido = PedidoLoja.query.get(pid) if pid else None

    pedido_atual = None
    if pedido:
        pedido_atual = {
            'id': pedido.id,
            'loja_nome': pedido.loja.nome if pedido.loja else '?',
            'data_entrega': pedido.data_entrega.strftime('%Y-%m-%d') if pedido.data_entrega else None,
            'observacao': pedido.observacao or '',
            'status': pedido.status,
            'itens': [
                {
                    'nome': it.nome_item,
                    'quantidade': it.quantidade,
                    'estado': it.estado,
                    'observacao': it.observacao or '',
                }
                for it in pedido.itens
            ],
        }

    itens_input = tool_input.get('itens')
    itens_enriq = None
    if itens_input is not None:
        # MPs que JA estao no pedido resolvem mesmo se hoje bloqueadas
        # (grandfather — o REPLACE re-envia a lista inteira e nao pode
        # derrubar item antigo legitimo).
        mp_ids_pedido = ({it.materia_prima_id for it in pedido.itens
                          if it.materia_prima_id} if pedido else set())
        itens_enriq = []
        for item in itens_input:
            nome = (item.get('nome') or '').strip()
            qtd = int(item.get('quantidade') or 0)
            if not nome or qtd <= 0:
                continue
            matches = _resolver_item_pedido(nome, mp_ids_extras=mp_ids_pedido)
            obs_item = (item.get('observacao') or '').strip() or None
            estado_item = (item.get('estado') or '').strip().lower() or None
            if estado_item not in (None, 'backup', 'assado'):
                estado_item = None
            itens_enriq.append({
                'nome_original': nome, 'quantidade': qtd,
                'observacao': obs_item, 'estado': estado_item,
                'matches': matches, 'resolvido': matches[0] if matches else None,
            })

    return {
        'pedido_id': pid,
        'pedido_atual': pedido_atual,
        'data_entrega': tool_input.get('data_entrega'),
        'observacao': tool_input.get('observacao'),
        'itens': itens_enriq,  # None = mantem; lista = REPLACE
    }


def _score_proximidade(query, nome):
    """Score pra desempatar fuzzy matches.

    Menor = melhor. Prefere (1) nome que comeca com a query, (2) nome
    mais curto (mais proximo da query em comprimento). Resolve casos
    como "sourdough" -> "Sourdough" vence "Mini Sourdough" e "pain au
    chocolat" -> "Pain au Chocolat" vence "Pain au Chocolat Bicolor".
    """
    q = (query or '').strip().lower()
    n = (nome or '').lower()
    starts_with = 0 if n.startswith(q) else 1
    diff_len = abs(len(n) - len(q))
    return (starts_with, diff_len)


def _rapidfuzz_top(query, choices, score_cutoff=60, limit=5):
    """Fallback semantico quando substring nao bate. Usa rapidfuzz
    token_set_ratio (robusto a ordem de palavras e abreviacoes). Retorna
    [(idx, score, choice)] ordenado por score desc, so acima do cutoff.
    choices = lista de strings (nomes).
    """
    try:
        from rapidfuzz import fuzz, process
    except ImportError:
        return []
    if not query or not choices:
        return []
    results = process.extract(
        query, choices, scorer=fuzz.token_set_ratio,
        limit=limit, score_cutoff=score_cutoff,
    )
    # results = [(choice, score, idx), ...]
    return [(idx, score, choice) for choice, score, idx in results]


def _separar_estado(nome):
    """'Croissant backup' -> ('Croissant', 'backup'); 'X assado' -> ('X','assado');
    'X cru'/'X' -> ('X', None). Deixa o copilot entender o estado pedido no nome
    do item da venda B2B (mesma ideia de PedidoItem.estado)."""
    import re
    nome = (nome or '').strip()
    m = re.search(r'\s+(backup|assado|cru)\s*$', nome, re.IGNORECASE)
    if not m:
        return nome, None
    est = m.group(1).lower()
    return nome[:m.start()].strip(), (None if est == 'cru' else est)


def _resolver_produto(nome):
    # Produto.ativo=True em TODOS os ramos (varredura 19/07/2026): o soft-
    # delete da UI (excluir com historico vira ativo=False) deixava o fuzzy
    # resolver produto morto pra pedido/venda NOVOS — Receita e MP ja
    # filtravam neste mesmo resolver.
    from sqlalchemy import func
    matches = []
    p = Produto.query.filter(func.lower(Produto.nome) == nome.lower(),
                             Produto.ativo.is_(True)).first()
    if p:
        matches.append({'tipo': 'produto', 'id': p.id, 'nome': p.nome, 'match': 'exato'})
    r = Receita.query.filter(func.lower(Receita.nome) == nome.lower(),
                             Receita.arquivada_em.is_(None)).first()
    if r:
        matches.append({'tipo': 'receita', 'id': r.id, 'nome': r.nome, 'match': 'exato'})
    if matches:
        return matches
    for p in (Produto.query.filter(Produto.nome.ilike(f'%{nome}%'),
                                   Produto.ativo.is_(True)).limit(10).all()):
        matches.append({'tipo': 'produto', 'id': p.id, 'nome': p.nome, 'match': 'fuzzy'})
    for r in (Receita.query.filter(Receita.nome.ilike(f'%{nome}%'),
                                   Receita.arquivada_em.is_(None)).limit(10).all()):
        matches.append({'tipo': 'receita', 'id': r.id, 'nome': r.nome, 'match': 'fuzzy'})
    if matches:
        matches.sort(key=lambda m: _score_proximidade(nome, m['nome']))
        return matches[:5]
    # Fallback rapidfuzz — quando nenhuma substring bate (ex: "PFR" vs
    # "Pao Frances Fermentado", "cro almnd" vs "Croissant Almond").
    produtos = Produto.query.filter(Produto.ativo.is_(True)).all()
    receitas = Receita.query.filter(Receita.arquivada_em.is_(None)).all()
    pool = [('produto', p.id, p.nome) for p in produtos] + \
           [('receita', r.id, r.nome) for r in receitas]
    if not pool:
        return []
    nomes = [n for _, _, n in pool]
    for idx, score, _ in _rapidfuzz_top(nome, nomes, score_cutoff=60, limit=5):
        tipo, _id, nome_real = pool[idx]
        matches.append({'tipo': tipo, 'id': _id, 'nome': nome_real,
                         'match': 'aproximado'})
    return matches


def _resolver_item_pedido(nome, mp_ids_extras=None):
    """Resolve nome em qualquer item que cabe num PedidoLoja: Receita,
    Produto OU MateriaPrima. Loja pede MPs tambem (queijo pra salada, lagarto
    cozido, saco de pao de queijo), entao tem que cobrir os 3.

    B2B e ajuste_estoque continuam usando `_resolver_produto` (so receita +
    produto), porque MP nao se aplica naqueles fluxos.

    So MPs LIBERADAS pra pedido de loja entram (checkbox "sugerir pedido
    loja" no Banco de MPs — decisao do dono 07/07/2026: loja pedia MP que
    nao devia). O receber_mp continua vendo todas via `_resolver_mp`.
    `mp_ids_extras`: ids liberados por excecao — o editar_pedido passa as
    MPs que JA estao no pedido (grandfather: re-enviar a lista atual nao
    pode derrubar um item antigo legitimo)."""
    matches = _resolver_produto(nome)
    mps = _resolver_mp(nome)
    if mps:
        liberadas = {mid for (mid,) in MateriaPrima.query
                     .with_entities(MateriaPrima.id)
                     .filter(MateriaPrima.id.in_([m['id'] for m in mps]),
                             MateriaPrima.sugerir_pedido_loja.is_(True))
                     .all()}
        if mp_ids_extras:
            liberadas |= set(mp_ids_extras)
        for m in mps:
            if m['id'] in liberadas:
                matches.append({'tipo': 'mp', 'id': m['id'], 'nome': m['nome'],
                                'match': m.get('match', 'fuzzy')})
    if not matches:
        return matches
    # dedup por (tipo, id) preservando ordem; exato primeiro.
    matches.sort(key=lambda m: 0 if m.get('match') == 'exato' else 1)
    vistos = set()
    out = []
    for m in matches:
        chave = (m['tipo'], m['id'])
        if chave in vistos:
            continue
        vistos.add(chave)
        out.append(m)
    return out[:5]


def _quantidade_recebimento_mp(params):
    """Quantidade FINAL (na unidade do CADASTRO da MP) de um receber_mp.

    Aceita `quantidade` (ja na unidade do cadastro) OU `quantidade_kg` (NF em
    kg): MP em 'un' converte via peso_unidade (8 kg de bolinha de 18g = 444
    un); MP em g/ml converte x1000. Retorna (quantidade, rotulo, erro) — o
    rotulo ("8 kg ~ 444 un de 18g") vai pro preview e pra referencia do
    movimento (auditoria). Ambiguidade (os dois campos) e ERRO: dinheiro e
    estoque nao adivinham."""
    resolvida = params.get('mp_resolvida') or {}
    try:
        qtd = float(params.get('quantidade') or 0)
    except (TypeError, ValueError):
        qtd = 0
    try:
        kg = float(params.get('quantidade_kg') or 0)
    except (TypeError, ValueError):
        kg = 0
    if qtd > 0 and kg > 0:
        return None, None, ('Informe quantidade OU quantidade_kg, nao os '
                            'dois — nao sei qual vale.')
    if qtd > 0:
        return qtd, None, None
    if kg <= 0:
        return None, None, 'Quantidade invalida'
    unidade = (resolvida.get('unidade') or '').lower()
    if unidade in ('g', 'ml'):
        return kg * 1000.0, f'{kg:g} kg = {kg * 1000.0:g} {unidade}', None
    if unidade == 'un':
        peso = float(resolvida.get('peso_unidade') or 0)
        if peso <= 0:
            return None, None, (f'"{resolvida.get("nome", "MP")}" e cadastrada '
                                'em un mas SEM peso por unidade — cadastre o '
                                'peso ou informe a quantidade em unidades.')
        unidades = int(round(kg * 1000.0 / peso))
        return float(unidades), f'{kg:g} kg \u2248 {unidades} un ({peso:g} g/un)', None
    return None, None, (f'Nao sei converter kg pra unidade '
                        f'"{unidade or "?"}" — informe a quantidade na '
                        'unidade do cadastro.')


def _resolver_mp(nome):
    from sqlalchemy import func
    matches = []
    m = MateriaPrima.ativas().filter(func.lower(MateriaPrima.nome) == nome.lower()).first()
    if m:
        matches.append({'id': m.id, 'nome': m.nome, 'unidade': m.unidade,
                        'peso_unidade': m.peso_unidade,
                        'observacoes': m.observacoes, 'match': 'exato'})
    if matches:
        return matches
    for m in MateriaPrima.ativas().filter(MateriaPrima.nome.ilike(f'%{nome}%')).limit(10).all():
        matches.append({'id': m.id, 'nome': m.nome, 'unidade': m.unidade,
                        'peso_unidade': m.peso_unidade,
                        'observacoes': m.observacoes, 'match': 'fuzzy'})
    if matches:
        matches.sort(key=lambda x: _score_proximidade(nome, x['nome']))
        return matches[:5]
    # Fallback rapidfuzz pra MPs com nome muito diferente do digitado.
    mps = MateriaPrima.ativas().all()
    if not mps:
        return []
    nomes = [m.nome for m in mps]
    for idx, score, _ in _rapidfuzz_top(nome, nomes, score_cutoff=60, limit=5):
        m = mps[idx]
        matches.append({'id': m.id, 'nome': m.nome, 'unidade': m.unidade,
                        'peso_unidade': m.peso_unidade,
                        'observacoes': m.observacoes, 'match': 'aproximado'})
    return matches


# ── Executores READ (sem aprovacao) ───────────────────────────────────

def _read_consultar_pedido(params, user):
    from app.constants import STATUS_PEDIDO_FINALIZADOS
    from app.models import PedidoLoja
    q = PedidoLoja.query
    if params.get('pedido_id'):
        p = q.filter_by(id=params['pedido_id']).first()
        if not p:
            return {'texto': f'Pedido #{params["pedido_id"]} nao encontrado.'}
        return {'texto': _formatar_pedido(p)}
    if params.get('loja_id') and user.is_admin():
        q = q.filter_by(loja_id=params['loja_id'])
    status_filtro = params.get('status')
    if status_filtro:
        q = q.filter_by(status=status_filtro)
    elif not params.get('incluir_finalizados'):
        q = q.filter(~PedidoLoja.status.in_(STATUS_PEDIDO_FINALIZADOS))
    try:
        if params.get('data_de'):
            q = q.filter(PedidoLoja.data_entrega >= datetime.strptime(params['data_de'], '%Y-%m-%d').date())
        if params.get('data_ate'):
            q = q.filter(PedidoLoja.data_entrega <= datetime.strptime(params['data_ate'], '%Y-%m-%d').date())
    except ValueError:
        pass

    formato = (params.get('formato') or 'lista').lower()
    limit = 50 if formato in ('detalhe', 'agregado') else 15
    pedidos = q.order_by(PedidoLoja.data_entrega.desc()).limit(limit).all()
    if not pedidos:
        return {'texto': 'Nenhum pedido pendente encontrado com esses filtros.'}

    if formato == 'agregado':
        return {'texto': _formatar_pedidos_agregado(pedidos)}
    if formato == 'detalhe':
        return {'texto': _formatar_pedidos_detalhe(pedidos)}

    from app.constants import STATUS_PEDIDO_LABEL
    linhas = [f'**{len(pedidos)} pedido(s) encontrado(s):**']
    for p in pedidos:
        label = STATUS_PEDIDO_LABEL.get(p.status, p.status)
        linhas.append(f'- #{p.id} · {p.loja.nome} · {p.data_entrega.strftime("%d/%m/%Y") if p.data_entrega else "—"} · {label} · {len(p.itens)} itens')
    return {'texto': '\n'.join(linhas)}


def _formatar_pedidos_detalhe(pedidos):
    """Lista cada pedido com seus itens (sem agregar)."""
    from collections import defaultdict

    from app.constants import STATUS_PEDIDO_LABEL
    por_data = defaultdict(list)
    for p in pedidos:
        por_data[p.data_entrega].append(p)

    linhas = [f'**{len(pedidos)} pedido(s):**']
    for data in sorted(por_data.keys(), reverse=True):
        if data:
            linhas.append(f'\n*Entrega {data.strftime("%d/%m/%Y")}*')
        for p in sorted(por_data[data], key=lambda x: (x.loja.nome if x.loja else '')):
            loja = p.loja.nome if p.loja else '?'
            label = STATUS_PEDIDO_LABEL.get(p.status, p.status)
            linhas.append(f'\n**#{p.id} · {loja} · {label}**')
            for it in p.itens:
                linhas.append(f'  - {it.quantidade}× {it.nome_item}')
    return '\n'.join(linhas)


def _formatar_pedidos_agregado(pedidos):
    """Estilo resumo Slack 04h: total do dia agregado por item + breakdown por loja.

    Se ha mais de uma data nos pedidos, agrupa por data.
    """
    from collections import defaultdict
    por_data = defaultdict(list)
    for p in pedidos:
        por_data[p.data_entrega].append(p)

    blocos = []
    for data in sorted(por_data.keys(), reverse=True):
        peds_data = por_data[data]
        data_label = data.strftime('%d/%m/%Y') if data else 'sem data'

        # Agrega por loja e total
        por_loja = defaultdict(lambda: defaultdict(int))  # loja → item → qtd
        total_item = defaultdict(int)
        for p in peds_data:
            loja = p.loja.nome if p.loja else '?'
            for it in p.itens:
                por_loja[loja][it.nome_item] += it.quantidade
                total_item[it.nome_item] += it.quantidade

        n = len(peds_data)
        qtd_total = sum(total_item.values())
        linhas = [f'**Entrega {data_label} — {n} pedido(s) · {qtd_total} unidades**']

        # Total do dia
        if total_item:
            linhas.append('\n*Producao total do dia:*')
            for nome, qtd in sorted(total_item.items(), key=lambda x: -x[1]):
                linhas.append(f'  • {qtd}× {nome}')

        # Por loja
        for loja in sorted(por_loja.keys()):
            itens_loja = por_loja[loja]
            n_itens = len(itens_loja)
            linhas.append(f'\n*{loja}* ({n_itens} {"itens" if n_itens != 1 else "item"})')
            for nome, qtd in sorted(itens_loja.items()):
                linhas.append(f'  • {qtd}× {nome}')

        blocos.append('\n'.join(linhas))

    return '\n\n---\n\n'.join(blocos)


def _formatar_pedido(p):
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
    """Consulta estoque em 4 escopos: mp, producao, loja, todos."""
    escopo = (params.get('escopo') or '').strip().lower()
    item_nome_legacy = (params.get('mp_nome') or '').strip()
    if not escopo and item_nome_legacy:
        escopo = 'mp'
    if not escopo:
        escopo = 'todos'

    item_nome = (params.get('item_nome') or item_nome_legacy or '').strip()
    apenas_baixo = bool(params.get('apenas_baixo'))
    loja_nome = (params.get('loja_nome') or '').strip()

    if escopo == 'mp':
        return _consultar_estoque_mp(item_nome, apenas_baixo)
    if escopo == 'producao':
        return _consultar_estoque_producao(item_nome)
    if escopo == 'loja':
        if loja_nome:
            return _consultar_estoque_loja(loja_nome, item_nome, user)
        # sem loja_nome → todas as lojas
        return _consultar_estoque_todas_lojas(item_nome)
    if escopo == 'todos':
        return _consultar_estoque_todos(item_nome)
    return {'texto': f'Escopo invalido: "{escopo}". Use mp, producao, loja ou todos.'}


def _consultar_estoque_todas_lojas(item_nome):
    """Lista estoque em TODAS as lojas, agrupado por loja."""
    from app.models import EstoqueLoja
    lojas = (Loja.query.filter(Loja.ativa.is_(True), Loja.nome != 'Industria')
             .order_by(Loja.nome).all())
    if not lojas:
        return {'texto': 'Nenhuma loja ativa.'}

    bloco = []
    total_geral = 0
    for loja in lojas:
        q = (EstoqueLoja.query.filter_by(loja_id=loja.id)
             .filter(EstoqueLoja.quantidade > 0).all())
        if item_nome:
            q = [el for el in q if item_nome.lower() in (el.nome_item or '').lower()]
        if not q:
            continue
        q.sort(key=lambda el: -(el.quantidade or 0))
        soma = sum((el.quantidade or 0) for el in q)
        total_geral += soma
        linhas = [f'  - {el.nome_item}: {el.quantidade}' for el in q[:20]]
        if len(q) > 20:
            linhas.append(f'  - _... +{len(q) - 20} itens_')
        bloco.append(f'*{loja.nome}* (total {soma}):\n' + '\n'.join(linhas))

    if not bloco:
        msg = (f'Nenhum item com "{item_nome}" em estoque em nenhuma loja.'
               if item_nome else 'Nenhuma loja com saldo positivo.')
        return {'texto': msg}

    cabecalho = ('**Estoque por loja' + (f' (filtro "{item_nome}")' if item_nome else '')
                  + f' — total {total_geral} un:**')
    return {'texto': cabecalho + '\n\n' + '\n\n'.join(bloco)}


def _consultar_estoque_todos(item_nome):
    """Visao geral: producao + todas as lojas. NAO inclui MP (uso so explicito)."""
    blocos = []

    res_prod = _consultar_estoque_producao(item_nome)
    txt = res_prod.get('texto', '')
    if 'vazio' not in txt.lower() and 'nenhum item' not in txt.lower():
        blocos.append(txt)

    res_lojas = _consultar_estoque_todas_lojas(item_nome)
    txt = res_lojas.get('texto', '')
    if 'nenhuma' not in txt.lower() and 'nenhum item' not in txt.lower():
        blocos.append(txt)

    if not blocos:
        msg = (f'Nenhum estoque com "{item_nome}" encontrado em industria ou lojas.'
               if item_nome else 'Estoque vazio em industria e lojas.')
        return {'texto': msg}
    return {'texto': '\n\n'.join(blocos)}


def _consultar_estoque_mp(item_nome, apenas_baixo):
    from app.models import AlertaEstoque
    if item_nome:
        matches = _resolver_mp(item_nome)
        if not matches:
            return {'texto': f'MP "{item_nome}" nao encontrada.'}
        m = MateriaPrima.query.get(matches[0]['id'])
        saldo = _calcular_saldo_mp(m.id)
        alerta = AlertaEstoque.query.filter_by(materia_prima_id=m.id).first()
        txt = f'**{m.nome}**: {saldo} {m.unidade or ""} em estoque.'
        if alerta and saldo < alerta.estoque_minimo:
            txt += f'\n:warning: ABAIXO do minimo ({alerta.estoque_minimo} {m.unidade}).'
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

    # Sem filtro: lista top com saldo positivo (limita 30)
    mps = MateriaPrima.ativas().order_by(MateriaPrima.nome).limit(80).all()
    if not mps:
        return {'texto': 'Nenhuma MP cadastrada.'}
    linhas = []
    for m in mps:
        saldo = _calcular_saldo_mp(m.id)
        if saldo > 0:
            linhas.append(f'- {m.nome}: {saldo} {m.unidade or ""}')
    if not linhas:
        return {'texto': 'Nenhuma MP com saldo positivo.'}
    return {'texto': f'**Estoque de MPs ({len(linhas)} itens com saldo):**\n'
                    + '\n'.join(linhas[:50])}


def _consultar_estoque_producao(item_nome):
    """Lista estoque da industria (EstoqueProducao). Receitas + Produtos."""
    from app.models import EstoqueProducao

    q = EstoqueProducao.query.filter(EstoqueProducao.quantidade > 0)
    itens = q.all()
    if not itens:
        return {'texto': 'Estoque da industria esta vazio.'}

    # Filtra por nome se fornecido
    if item_nome:
        def _match(ep):
            n = (ep.nome_item or '').lower()
            return item_nome.lower() in n
        itens = [ep for ep in itens if _match(ep)]
        if not itens:
            return {'texto': f'Nenhum item da industria com "{item_nome}".'}

    itens.sort(key=lambda ep: -(ep.quantidade or 0))
    linhas = [f'- {ep.nome_item}: {ep.quantidade}' for ep in itens[:50]]
    cabecalho = f'**Estoque da industria** ({len(itens)} item{"" if len(itens) == 1 else "ns"} com saldo):'
    if len(itens) > 50:
        linhas.append(f'_... +{len(itens) - 50} itens_')
    return {'texto': cabecalho + '\n' + '\n'.join(linhas)}


def _consultar_estoque_loja(loja_nome, item_nome, user):
    """Lista estoque de uma loja especifica (EstoqueLoja)."""
    from app.models import EstoqueLoja

    # Resolve loja
    loja = None
    if loja_nome:
        from sqlalchemy import func
        loja = Loja.query.filter(func.lower(Loja.nome) == loja_nome.lower()).first()
        if not loja:
            loja = Loja.query.filter(Loja.nome.ilike(f'%{loja_nome}%')).first()

    if not loja:
        return {'texto': f'Loja "{loja_nome}" nao encontrada. Informe o nome correto.'}

    q = EstoqueLoja.query.filter_by(loja_id=loja.id).filter(EstoqueLoja.quantidade > 0)
    itens = q.all()
    if not itens:
        return {'texto': f'Estoque da {loja.nome} esta vazio.'}

    if item_nome:
        def _match(el):
            return item_nome.lower() in (el.nome_item or '').lower()
        itens = [el for el in itens if _match(el)]
        if not itens:
            return {'texto': f'Nenhum item com "{item_nome}" na {loja.nome}.'}

    itens.sort(key=lambda el: -(el.quantidade or 0))
    linhas = [f'- {el.nome_item}: {el.quantidade}' for el in itens[:50]]
    cabecalho = f'**Estoque da {loja.nome}** ({len(itens)} item{"" if len(itens) == 1 else "ns"} com saldo):'
    if len(itens) > 50:
        linhas.append(f'_... +{len(itens) - 50} itens_')
    return {'texto': cabecalho + '\n' + '\n'.join(linhas)}


def _calcular_saldo_mp(mp_id):
    from sqlalchemy import func as sqlfunc

    from app.models import MovimentacaoEstoque
    entradas = db.session.query(sqlfunc.coalesce(sqlfunc.sum(MovimentacaoEstoque.quantidade), 0)) \
        .filter_by(materia_prima_id=mp_id, tipo='entrada').scalar() or 0
    saidas = db.session.query(sqlfunc.coalesce(sqlfunc.sum(MovimentacaoEstoque.quantidade), 0)) \
        .filter_by(materia_prima_id=mp_id, tipo='saida').scalar() or 0
    return round(entradas - saidas, 3)


# ── Executores WRITE (aprovacao obrigatoria) ──────────────────────────

def executar_criar_pedido(params, user):
    from app.models import PedidoItem, PedidoLoja
    loja_id = params.get('loja_id')
    if not loja_id:
        return {'ok': False, 'erro': ('Loja nao especificada. Diga o nome '
                                       '(ex: "criar pedido pra loja Ribeiro '
                                       'do Vale amanha com 50 paes").')}
    loja = Loja.query.get(loja_id)
    if not loja:
        return {'ok': False, 'erro': f'Loja {loja_id} nao encontrada. Verifique o nome.'}
    try:
        data_entrega = datetime.strptime(params.get('data_entrega'), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return {'ok': False, 'erro': 'Data invalida'}
    itens = params.get('itens') or []
    if not itens:
        return {'ok': False, 'erro': 'Pedido sem itens'}

    # Normaliza os itens resolvidos (tipo->FK) — comum pro merge e pro novo.
    itens_norm = []
    nao_resolvidos = []
    for item in itens:
        qtd = int(item.get('quantidade') or 0)
        if qtd <= 0:
            continue
        resolvido = item.get('resolvido')
        if not resolvido or not resolvido.get('id'):
            nao_resolvidos.append(item.get('nome_original') or '?')
            continue
        # Estado vem da Claude (`backup`/`assado`) ou null = padrao da familia.
        estado_item = (item.get('estado') or '').strip().lower() or None
        if estado_item not in (None, 'backup', 'assado'):
            estado_item = None
        itens_norm.append({
            'receita_id': resolvido['id'] if resolvido['tipo'] == 'receita' else None,
            'produto_id': resolvido['id'] if resolvido['tipo'] == 'produto' else None,
            'materia_prima_id': resolvido['id'] if resolvido['tipo'] == 'mp' else None,
            'quantidade': qtd,
            'observacao': (item.get('observacao') or '').strip()[:200] or None,
            'estado': estado_item,
        })
    if not itens_norm:
        return {'ok': False, 'erro': f'Nenhum item resolvido. Nao achei: {", ".join(nao_resolvidos)}'}

    # MP so entra em pedido de loja se estiver liberada no Banco de MPs
    # (checkbox "sugerir pedido loja" — decisao do dono 07/07/2026). O
    # resolver ja filtra, mas preview antigo/params re-enviados nao podem
    # furar a trava.
    mp_ids = [it['materia_prima_id'] for it in itens_norm if it['materia_prima_id']]
    if mp_ids:
        bloqueadas = (MateriaPrima.query
                      .filter(MateriaPrima.id.in_(mp_ids),
                              MateriaPrima.sugerir_pedido_loja.is_(False))
                      .all())
        if bloqueadas:
            nomes = ', '.join(m.nome for m in bloqueadas)
            return {'ok': False, 'erro': (
                f'Materia(s)-prima(s) nao liberada(s) pra pedido de loja: '
                f'{nomes}. Um admin pode liberar no Banco de MPs '
                f'(checkbox "sugerir pedido loja").')}

    # Item em g/ml com lote definido so aceita MULTIPLO do lote (iogurte
    # 3000 / granola 5000 — dono 18/08/2026, caso "potes"). Espelho da
    # tela web; no executor pra preview re-enviado nao furar.
    from app.services.pedido_lote import violacoes_por_ids
    fora_do_lote = violacoes_por_ids(itens_norm)
    if fora_do_lote:
        return {'ok': False, 'erro': ' | '.join(fora_do_lote)}

    # Corte do fim do dia (dono 10/08/2026): pedido pra AMANHA fecha na
    # HORA_CORTE —
    # e o horario de corte do pre-preparo do padeiro. Espelho da tela web,
    # checado no EXECUTOR (preview re-enviado nao fura); admin passa com
    # aviso no resultado. ANTES do merge — "criar" pode virar mesclar num
    # pedido de amanha ja existente.
    from app.services.pedido_corte import bloqueio_do_corte
    bloqueado_corte, aviso_corte = bloqueio_do_corte([data_entrega], user=user)
    if bloqueado_corte:
        return {'ok': False, 'erro': aviso_corte}

    # Ja existe pedido aberto da loja nessa data? Junta nele em vez de duplicar.
    from app.services.pedido_merge import (
        absorver_rascunho_automatico,
        adotar_rascunho_automatico,
        mesclar_itens,
        pedido_aberto_para_merge,
        rascunho_automatico_aberto,
    )
    alvo = pedido_aberto_para_merge(loja_id, data_entrega, 'confirmado')
    if alvo:
        res = mesclar_itens(alvo, itens_norm, modificado_por_id=user.id)
        absorvido = absorver_rascunho_automatico(loja_id, data_entrega, user.id)
        db.session.commit()
        out = {'ok': True, 'pedido_id': alvo.id, 'mesclado': True,
               'itens_salvos': res['adicionados'] + res['somados'],
               'nao_resolvidos': nao_resolvidos, 'registro_tipo': 'pedido_loja',
               'registro_id': alvo.id, 'url': f'/pedidos/{alvo.id}'}
        avisos = [a for a in (aviso_corte,) if a]
        if absorvido is not None:
            avisos.append(f'O rascunho automático #{absorvido.id} do mesmo '
                          'dia foi cancelado — o pedido da loja manda.')
        if avisos:
            out['aviso'] = ' | '.join(avisos)
        return out

    # Dia coberto pelo CRON de auto-pedidos (10/08/2026): adota o rascunho
    # em vez de criar um segundo pedido (2 pedidos no mesmo dia = producao
    # em dobro na ordem enviada no corte). Item citado SUBSTITUI a quantidade do
    # motor; item do motor nao citado FICA no pedido (o aviso lista).
    rascunho = rascunho_automatico_aberto(loja_id, data_entrega)
    if rascunho is not None:
        res_adote = adotar_rascunho_automatico(
            rascunho, itens_norm, user.id,
            observacao=(params.get('observacao') or '').strip() or None)
        db.session.commit()
        out = {'ok': True, 'pedido_id': rascunho.id, 'adotou_rascunho': True,
               'itens_salvos': res_adote['substituidos'] + res_adote['adicionados'],
               'nao_resolvidos': nao_resolvidos, 'registro_tipo': 'pedido_loja',
               'registro_id': rascunho.id, 'url': f'/pedidos/{rascunho.id}'}
        avisos = [a for a in (aviso_corte,) if a]
        avisos.append('Este dia já tinha a sugestão automática do sistema: '
                      'suas quantidades substituíram as sugeridas'
                      + (f' e {res_adote["mantidos"]} item(ns) da sugestão '
                         'que você não citou foram MANTIDOS no pedido'
                         if res_adote['mantidos'] else '')
                      + f' (pedido #{rascunho.id}, confirmado).')
        out['aviso'] = ' | '.join(avisos)
        return out

    pedido = PedidoLoja(
        loja_id=loja_id, data_entrega=data_entrega,
        observacao=(params.get('observacao') or '').strip() or None,
        criado_por=user.id, status='confirmado',
    )
    db.session.add(pedido)
    db.session.flush()
    for it in itens_norm:
        db.session.add(PedidoItem(pedido_id=pedido.id, **it))
    db.session.commit()
    # Alerta Slack se for emergencia (criado hoje pra entrega hoje)
    try:
        from app.services.slack_resumos import alertar_pedido_emergencia
        alertar_pedido_emergencia(pedido)
    except Exception:  # noqa: BLE001
        logger.exception('Alerta emergencia falhou (copilot)')
    out = {'ok': True, 'pedido_id': pedido.id, 'itens_salvos': len(itens_norm),
           'nao_resolvidos': nao_resolvidos, 'registro_tipo': 'pedido_loja',
           'registro_id': pedido.id, 'url': f'/pedidos/{pedido.id}'}
    if aviso_corte:
        out['aviso'] = aviso_corte
    return out


def executar_editar_pedido(params, user):
    """Edita pedido existente. Aceita data_entrega, observacao, e itens (REPLACE
    total). NAO mexe em loja/driver/status. Bloqueia se status fora de
    pendente/confirmado."""
    from app.models import PedidoItem, PedidoLoja
    pid = params.get('pedido_id')
    if not pid:
        return {'ok': False, 'erro': 'pedido_id obrigatorio'}
    pedido = PedidoLoja.query.get(pid)
    if not pedido:
        return {'ok': False, 'erro': f'Pedido {pid} nao encontrado.'}
    if pedido.status not in ('pendente', 'confirmado'):
        return {'ok': False, 'erro': f'Pedido {pid} em status "{pedido.status}" — nao pode ser editado. Cancele e recrie.'}

    mudancas = []

    nova_data = params.get('data_entrega')
    # Corte do fim do dia (dono 10/08/2026): olha a data ATUAL e a NOVA —
    # mover um pedido PRA amanha (ou tirar de amanha) depois do corte muda
    # o pre-preparo igual. Espelho da tela web, no executor.
    from app.services.pedido_corte import bloqueio_do_corte
    _datas_corte = [pedido.data_entrega]
    if nova_data:
        try:
            _datas_corte.append(
                datetime.strptime(nova_data, '%Y-%m-%d').date())
        except (ValueError, TypeError):
            pass                       # data invalida ja falha logo abaixo
    bloqueado_corte, aviso_corte = bloqueio_do_corte(_datas_corte, user=user)
    if bloqueado_corte:
        return {'ok': False, 'erro': aviso_corte}

    aviso_rascunho = None
    if nova_data:
        try:
            d = datetime.strptime(nova_data, '%Y-%m-%d').date()
            if d != pedido.data_entrega:
                pedido.data_entrega = d
                mudancas.append('data_entrega')
                # Mover o pedido pra um dia que o cron de auto-pedidos já
                # cobriu deixaria rascunho + pedido humano no mesmo dia
                # (demanda em dobro) — o rascunho vira redundância e cai.
                from app.services.pedido_merge import (
                    absorver_rascunho_automatico,
                )
                absorvido = absorver_rascunho_automatico(
                    pedido.loja_id, d, user.id, excluir_id=pedido.id)
                if absorvido is not None:
                    aviso_rascunho = (
                        f'O rascunho automático #{absorvido.id} do dia de '
                        'destino foi cancelado — o seu pedido manda.')
        except (ValueError, TypeError):
            return {'ok': False, 'erro': f'Data invalida: {nova_data}'}

    nova_obs = params.get('observacao')
    if nova_obs is not None:
        novo_val = (nova_obs or '').strip() or None
        if novo_val != pedido.observacao:
            pedido.observacao = novo_val
            mudancas.append('observacao')

    itens_novos = params.get('itens')
    nao_resolvidos = []
    if itens_novos is not None:
        # MP NOVA so entra se liberada no Banco de MPs (checkbox "sugerir
        # pedido loja"); MP que JA estava no pedido segue valida
        # (grandfather, igual a tela web). Checado ANTES do REPLACE.
        mp_ids_antes = {it.materia_prima_id for it in pedido.itens
                        if it.materia_prima_id}
        mp_ids_novos = [it['resolvido']['id'] for it in itens_novos
                        if it.get('resolvido')
                        and it['resolvido'].get('tipo') == 'mp'
                        and it['resolvido'].get('id')
                        and it['resolvido']['id'] not in mp_ids_antes]
        if mp_ids_novos:
            bloqueadas = (MateriaPrima.query
                          .filter(MateriaPrima.id.in_(mp_ids_novos),
                                  MateriaPrima.sugerir_pedido_loja.is_(False))
                          .all())
            if bloqueadas:
                nomes = ', '.join(m.nome for m in bloqueadas)
                return {'ok': False, 'erro': (
                    f'Materia(s)-prima(s) nao liberada(s) pra pedido de '
                    f'loja: {nomes}. Um admin pode liberar no Banco de MPs '
                    f'(checkbox "sugerir pedido loja").')}
        # Item em g/ml com lote definido so aceita MULTIPLO do lote
        # (iogurte 3000 / granola 5000 — dono 18/08/2026). Checado ANTES
        # do REPLACE; sem grandfather de proposito (decisao do dono: o
        # 9360 antigo vira 9000/12000 ao editar).
        from app.services.pedido_lote import violacoes_por_ids
        _itens_lote = [
            {'receita_id': it['resolvido']['id'],
             'quantidade': it.get('quantidade')}
            for it in itens_novos
            if it.get('resolvido')
            and it['resolvido'].get('tipo') == 'receita'
            and it['resolvido'].get('id')]
        fora_do_lote = violacoes_por_ids(_itens_lote)
        if fora_do_lote:
            return {'ok': False, 'erro': ' | '.join(fora_do_lote)}
        # REPLACE total. Deletar VIA ORM (não Query.delete em massa) pra
        # disparar o cascade 'all, delete-orphan' das fotos de conferência
        # (pedido_item_foto) — o bulk delete pula o cascade e bate na FK
        # (sem ON DELETE CASCADE), quebrando editar pedido que já tem foto.
        for _it in list(pedido.itens):
            db.session.delete(_it)
        db.session.flush()
        salvos = 0
        for item in itens_novos:
            qtd = int(item.get('quantidade') or 0)
            if qtd <= 0:
                continue
            resolvido = item.get('resolvido')
            if not resolvido or not resolvido.get('id'):
                nao_resolvidos.append(item.get('nome_original') or '?')
                continue
            obs_item = (item.get('observacao') or '').strip()[:200] or None
            estado_item = (item.get('estado') or '').strip().lower() or None
            if estado_item not in (None, 'backup', 'assado'):
                estado_item = None
            pi = PedidoItem(pedido_id=pedido.id, quantidade=qtd,
                            observacao=obs_item, estado=estado_item)
            if resolvido['tipo'] == 'produto':
                pi.produto_id = resolvido['id']
            elif resolvido['tipo'] == 'receita':
                pi.receita_id = resolvido['id']
            elif resolvido['tipo'] == 'mp':
                pi.materia_prima_id = resolvido['id']
            db.session.add(pi)
            salvos += 1
        if salvos == 0:
            db.session.rollback()
            return {'ok': False, 'erro': f'Nenhum item resolvido. Nao achei: {", ".join(nao_resolvidos)}'}
        mudancas.append(f'itens ({salvos})')

    if not mudancas:
        return {'ok': False, 'erro': 'Nada pra mudar (params iguais ao atual).'}

    pedido.modificado_em = agora()
    pedido.modificado_por_id = user.id
    db.session.commit()
    out = {'ok': True, 'pedido_id': pedido.id, 'mudancas': mudancas,
           'nao_resolvidos': nao_resolvidos,
           'registro_tipo': 'pedido_loja', 'registro_id': pedido.id,
           'url': f'/pedidos/{pedido.id}'}
    # Admin editando sob o corte: passa, mas o aviso vai junto (mesmo
    # contrato do executar_criar_pedido). Idem pro rascunho absorvido.
    avisos = [a for a in (aviso_corte, aviso_rascunho) if a]
    if avisos:
        out['aviso'] = ' | '.join(avisos)
    return out


def executar_receber_mp(params, user):
    from app.models import MovimentacaoEstoque
    resolvida = params.get('mp_resolvida')
    if not resolvida or not resolvida.get('id'):
        return {'ok': False, 'erro': f'MP nao identificada: {params.get("mp_nome")}'}
    # Converte quantidade_kg -> unidade do cadastro (recalcula aqui, nao confia
    # no param enriquecido — dinheiro/estoque).
    quantidade, rotulo, erro = _quantidade_recebimento_mp(params)
    if erro:
        return {'ok': False, 'erro': erro}
    quantidade = float(quantidade or 0)
    if quantidade <= 0:
        return {'ok': False, 'erro': 'Quantidade invalida'}
    preco_unitario = params.get('preco_unitario')
    preco_total = params.get('preco_total')
    if preco_total and not preco_unitario:
        # Por unidade JA CONVERTIDA (ex: R$ 51,80 do saco / 111 un = por bolinha).
        preco_unitario = float(preco_total) / quantidade
    referencia = (params.get('referencia') or '').strip() or None
    if rotulo:
        referencia = f'{referencia} [{rotulo}]' if referencia else f'[{rotulo}]'
    mov = MovimentacaoEstoque(
        materia_prima_id=resolvida['id'], tipo='entrada',
        quantidade=quantidade,
        preco_unitario=float(preco_unitario) if preco_unitario else None,
        referencia=referencia,
        usuario_id=user.id,
    )
    db.session.add(mov)
    # Mantem o denormalizado em sincronia com o movimento — a saida de pedido
    # pra loja baixa mp.estoque_atual; sem isto a entrada via bot nao aparecia
    # no estoque operacional (as rotas manuais sempre atualizaram os dois).
    mp_obj = MateriaPrima.query.get(resolvida['id'])
    if mp_obj:
        mp_obj.estoque_atual = (mp_obj.estoque_atual or 0) + quantidade
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
    # Sincroniza o denormalizado (mesma razao do executar_receber_mp).
    mp_obj = MateriaPrima.query.get(resolvida['id'])
    if mp_obj:
        if tipo == 'entrada':
            mp_obj.estoque_atual = (mp_obj.estoque_atual or 0) + quantidade
        else:
            mp_obj.estoque_atual = max(0, (mp_obj.estoque_atual or 0) - quantidade)
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
    r = Receita.query.filter(Receita.nome.ilike(nome),
                             Receita.arquivada_em.is_(None)).first()
    if not r:
        # Tenta produto — SEM filtro de ativo: consultar margem e LEITURA
        # (contrato do catalogo.py: filtro so em picker/matcher).
        p = Produto.query.filter(Produto.nome.ilike(nome)).first()
        if not p:
            return {'texto': f'"{nome}" nao encontrado.'}
        return {'texto': f'**{p.nome}** (produto): atacado R$ {p.preco_atacado or 0:.2f}, loja R$ {p.preco_loja or 0:.2f}, site R$ {p.preco_site or 0:.2f}.'}
    from app.services import impostos
    custo_un = custos.get(r.nome, 0)
    rendimento = calcular_rendimento(r)
    alq = impostos.aliquotas()
    carga = alq['total'] / 100.0
    linhas = [f'**{r.nome}** (receita)']
    linhas.append(f'- Custo unitário: R$ {custo_un:.4f}')
    linhas.append(f'- Rendimento: {rendimento}')
    linhas.append(f'- Impostos sobre venda: {alq["total"]:.2f}% '
                  f'(PIS {alq["pis"]:.2f} + COFINS {alq["cofins"]:.2f} '
                  f'+ ICMS {alq["icms"]:.2f}) — margens abaixo são líquidas')
    for label, preco in [('Atacado', r.preco_venda), ('Loja', r.preco_loja), ('Site', r.preco_site)]:
        if preco:
            m = impostos.margem_liquida(preco, custo_un, carga)
            linhas.append(f'- {label}: R$ {preco:.2f} (margem líq. {m:.1f}%)'
                          if m is not None else f'- {label}: R$ {preco:.2f}')
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


def _valor_mov_estoque(m):
    """Valor REAL de uma MovimentacaoEstoque, respeitando a unidade da MP.

    `quantidade` esta na unidade de estoque da MP (g/ml/kg/un); `preco_unitario`
    eh sempre por kg (ou por un quando a MP eh 'un'). Multiplicar direto sem
    fator inflava 1000x pra MPs em g/ml (caso real 19/06/2026: bot reportou
    R$ 1.637.220 em 2 compras — eram R$ 1.637,22). Mesma logica de
    `custos.py:_custo_unitario_mov` (custo / 1000 pra g/ml)."""
    qtd = float(m.quantidade or 0)
    preco = float(m.preco_unitario or 0)
    mp = getattr(m, 'materia_prima', None)
    unidade = (getattr(mp, 'unidade', '') or '').lower()
    if unidade in ('g', 'ml'):
        return qtd * preco / 1000.0
    return qtd * preco   # 'kg' e 'un' multiplicam direto


def _read_consultar_caixa(params, user):
    from sqlalchemy import func as sqlfunc

    from app.models import AtribuicaoEntrega, MovimentacaoEstoque, PedidoLocal
    from app.utils import hoje as _hoje
    data_str = params.get('data')
    try:
        d = datetime.strptime(data_str, '%Y-%m-%d').date() if data_str else _hoje()
    except ValueError:
        d = _hoje()
    locais = PedidoLocal.query.filter(PedidoLocal.data_entrega == d).all()
    valor_locais = sum(p.valor_total for p in locais)
    atribs = AtribuicaoEntrega.query.filter(AtribuicaoEntrega.data_entrega == d).all()
    movs = MovimentacaoEstoque.query.filter(
        MovimentacaoEstoque.tipo == 'entrada',
        sqlfunc.date(MovimentacaoEstoque.data) == d,
    ).all()
    valor_compras = sum(_valor_mov_estoque(m) for m in movs)
    linhas = [f'**Resumo de {d.strftime("%d/%m/%Y")}:**']
    linhas.append(f'- {len(locais)} pedido(s) local → R$ {valor_locais:.2f}')
    feitas = sum(1 for a in atribs if a.status == 'entregue')
    falhas = sum(1 for a in atribs if a.status == 'nao_entregue')
    linhas.append(f'- {len(atribs)} entregas atribuidas ({feitas} feitas, {falhas} falhas)')
    linhas.append(f'- {len(movs)} compras de MP → R$ {valor_compras:.2f}')
    # Breakdown por MP (top por valor). Ajuda a achar entrada errada no banco
    # — se um item domina o total, da pra ver na hora qual investigar.
    if movs:
        por_mp = {}
        for m in movs:
            nome = getattr(m.materia_prima, 'nome', '?') or '?'
            por_mp[nome] = por_mp.get(nome, 0) + _valor_mov_estoque(m)
        top = sorted(por_mp.items(), key=lambda x: -x[1])[:5]
        for nome, val in top:
            linhas.append(f'    • {nome} → R$ {val:.2f}')
        if len(por_mp) > 5:
            linhas.append(f'    • (+ {len(por_mp) - 5} outros)')
    return {'texto': '\n'.join(linhas)}


def _read_prever_pedido(params, user):
    """Previsao de pedido de reposicao por loja (media semanal do historico
    de PedidoLoja). Conta determinista server-side — o modelo so apresenta."""
    from app.services.previsao_demanda import prever_pedido_por_loja
    from app.utils import resolver_loja_por_nome

    try:
        semanas = int(params.get('semanas') or 3)
    except (TypeError, ValueError):
        semanas = 3
    semanas = max(1, min(semanas, 12))
    data_ref = (params.get('data_ref') or '').strip() or None

    loja_nome = (params.get('loja') or '').strip() or None
    loja_id = None
    if loja_nome:
        loja_obj = resolver_loja_por_nome(loja_nome)
        if not loja_obj:
            return {'texto': f'Não achei a loja "{loja_nome}". '
                             'Tente o nome como aparece no sistema.'}
        loja_id = loja_obj.id

    prev = prever_pedido_por_loja(semanas=semanas, data_ref=data_ref,
                                   loja_id=loja_id)
    if not prev:
        return {'texto': f'Nenhum pedido nas últimas {semanas} semana(s) '
                         'pra calcular a previsão.'}

    blocos = []
    for lid in sorted(prev, key=lambda k: prev[k]['loja_nome']):
        d = prev[lid]
        linhas = [
            f'**{d["loja_nome"]}** — previsão p/ a semana que vem '
            f'(base: {d["pedidos_considerados"]} pedido(s) em {d["semanas"]} '
            f'semana(s), {d["desde"]} a {d["ate"]})']
        for it in d['itens']:
            linhas.append(
                f'  • {it["sugerido"]}× {it["nome"]} '
                f'(média {it["media_semanal"]}/sem, {it["total"]} no período)')
        blocos.append('\n'.join(linhas))
    return {'texto': '\n\n'.join(blocos)}


def _read_consultar_vendas_itens(params, user):
    """Agrega itens vendidos da Seru no intervalo (e loja opcional)."""
    from app.services import vendas_itens
    from app.utils import hoje as _hoje_brt
    hoje = _hoje_brt()
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
        if loja:
            from app.services.loja_pagamento import loja_origem_site
            from app.utils import resolver_loja_por_nome
            loja_obj = resolver_loja_por_nome(loja)
            lv = loja_origem_site()
            if loja_obj and lv and loja_obj.id == lv.id:
                # Loja do site (Anesio): nao tem PDV Seru — vendas vem da
                # loja propria (PedidoOnline).
                data = vendas_itens.vendas_vnda_loja(ini, fim)
                fonte_label = 'e-commerce/site'
            else:
                # Filtro por loja Seru — le do BANCO (VendaSeruDiaria), sem
                # re-consultar a API.
                from app.services import vendas_diarias
                data = vendas_diarias.agregar_flat(ini, fim, loja_seru=loja)
                fonte_label = 'PDV/Seru'
        else:
            # Sem filtro de loja → consolida Seru + site (loja propria) + VNDA
            data = vendas_itens.agregar_itens_consolidado(ini, fim)
            fonte_label = 'PDV/Seru + e-commerce/site'
    except Exception as e:
        logger.exception('consultar_vendas_itens falhou')
        return {'erro': f'{type(e).__name__}: {str(e)[:300]}'}

    if not data['produtos']:
        sufixo = f' na loja "{loja}"' if loja else ''
        return {'texto': f'Nenhuma venda encontrada de {ini.strftime("%d/%m")} a {fim.strftime("%d/%m")}{sufixo}.'}

    cab = f'**Vendas {ini.strftime("%d/%m")} → {fim.strftime("%d/%m")}** ({fonte_label})'
    if loja:
        cab += f' · {loja}'
    if 'total_pedidos' in data:
        cab += f' · {data["total_pedidos"]} pedido(s)'
    cab += f' · R$ {data["faturamento_total"]:.2f}'
    if data.get('faturamento_fonte') == 'seru_apenas':
        cab += ' _(faturamento so Seru)_'

    linhas = [cab, '']
    for i, p in enumerate(data['produtos'][:top], 1):
        match_str = ''
        if p.get('match'):
            kind = ' (fuzzy)' if p['match']['kind'] == 'fuzzy' else ''
            match_str = f' ↔ {p["match"]["nome"]}{kind}'
        else:
            match_str = ' ⚠ sem match no sistema'
        # Tag de canais so quando o item vendeu por mais de uma fonte
        # (Seru/VNDA/site). Fonte unica dispensa tag (o cabecalho ja diz).
        partes = []
        if int(p.get('qtd_seru', 0) or 0):
            partes.append(f'Seru {int(p["qtd_seru"])}')
        if int(p.get('qtd_vnda', 0) or 0):
            partes.append(f'VNDA {int(p["qtd_vnda"])}')
        if int(p.get('qtd_online', 0) or 0):
            partes.append(f'site {int(p["qtd_online"])}')
        fonte_tag = f' [{" + ".join(partes)}]' if len(partes) > 1 else ''
        fat = p.get('faturamento') or 0
        fat_str = f' · R$ {fat:.2f}' if fat else ''
        linhas.append(
            f'{i}. **{p["nome"]}** — {int(p["qtd"])} un{fonte_tag}{fat_str}{match_str}'
        )
    if data.get('sem_match_count'):
        linhas.append('')
        linhas.append(f'_{data["sem_match_count"]} produto(s) Seru sem match no cadastro._')
    if data.get('vnda_aviso'):
        linhas.append('')
        linhas.append(f'_Aviso VNDA: {data["vnda_aviso"]}_')
    if not loja and data.get('lojas_no_intervalo'):
        linhas.append('')
        linhas.append('Lojas Seru no intervalo: ' + ', '.join(data['lojas_no_intervalo']))
    return {'texto': '\n'.join(linhas)}


# ───── Tools WRITE novas ───────────────────────────────────────────────

def executar_mudar_status_pedido(params, user):
    from app.models import (
        EstoqueLoja,
        MovEstoqueLoja,
        PedidoLoja,
    )
    pid = params.get('pedido_id')
    novo = params.get('novo_status')
    p = PedidoLoja.query.get(pid)
    if not p:
        return {'ok': False, 'erro': f'Pedido #{pid} nao encontrado'}

    transicoes = {
        'confirmar': ('pendente', 'confirmado'),
        # Pode separar a partir de qualquer estado anterior (admin nao
        # precisa passar por 'confirmar' antes).
        'separar': (('pendente', 'confirmado'), 'separado'),
        # Enviar aceita qualquer estado anterior — sistema "queima" as etapas
        # intermediarias. Se o usuario disser 'envia o pedido' direto do
        # pendente, o sistema entende que o admin pulou separacao no copilot.
        'enviar': (('pendente', 'confirmado', 'separado'), 'em_transporte'),
        # Receber aceita pular se nao passou por em_transporte (ex: admin
        # confirma recebimento direto pelo Slack sem QR).
        'receber': (('separado', 'em_transporte'), 'entregue'),
        'cancelar': (('pendente', 'confirmado', 'separado', 'em_transporte'), 'cancelado'),
    }
    if novo not in transicoes:
        return {'ok': False, 'erro': f'status invalido: {novo}'}
    de, para = transicoes[novo]
    if isinstance(de, str):
        de = (de,)
    if p.status not in de:
        STATUS_LABEL = {
            'pendente': 'pedido feito', 'confirmado': 'pedido feito',
            'separado': 'enviado', 'em_transporte': 'enviado',
            'entregue': 'recebido', 'cancelado': 'cancelado',
        }
        ACAO_LABEL = {'enviar': 'enviar', 'receber': 'marcar como recebido',
                      'separar': 'enviar', 'confirmar': 'confirmar',
                      'cancelar': 'cancelar'}
        return {'ok': False,
                'erro': f'Pedido #{pid} ja esta {STATUS_LABEL.get(p.status, p.status)}, nao pode {ACAO_LABEL.get(novo, novo)} novamente.'}

    # ENTREGA EXIGE FOTO (decisao do dono 13/06/2026). O recebimento por
    # texto (Slack/WhatsApp) nao tem como anexar foto, entao o copilot NAO
    # fecha entrega — redireciona pro app, onde a foto e obrigatoria
    # (_executar_recebimento_pedido valida). Sem isso o copilot seria um
    # furo na regra (fechava entrega sem comprovacao). NAO marca status
    # nem mexe em estoque — so devolve a orientacao.
    if novo == 'receber':
        return {'ok': False, 'redirecionar': True,
                'pedido_id': pid, 'url': f'/pedidos/{pid}',
                'erro': (f'Pra confirmar o recebimento do pedido #{pid} '
                         'agora é obrigatório anexar foto do pedido recebido. '
                         'Abra a ficha do pedido no app e confirme com a foto — '
                         f'/pedidos/{pid}')}

    # Corte do fim do dia (dono 10/08/2026): CANCELAR o pedido de amanhã
    # depois do corte muda o pré-preparo já calculado — mesmo bloqueio da rota web
    # de cancelar (a revisão de 13/08 pegou este executor sem o check).
    aviso_corte = None
    if novo == 'cancelar':
        from app.services.pedido_corte import bloqueio_do_corte
        bloqueado_corte, aviso_corte = bloqueio_do_corte(
            [p.data_entrega], user=user)
        if bloqueado_corte:
            return {'ok': False, 'erro': aviso_corte}

    try:
        # ENVIAR: baixa estoque da industria pelo MOTOR ÚNICO (03/07/2026) —
        # mesma função da rota web/QR (get-or-create da linha + quantidade
        # real + falta registrada). Antes era uma cópia inline que pulava
        # item sem linha em silêncio.
        if novo == 'enviar':
            from app.services.pedido_estoque import baixar_industria_pedido
            baixar_industria_pedido(p, user.id, ref_extra='copilot')

        # CANCELAR um pedido que JÁ SAIU (em_transporte): estorna a baixa da
        # industria antes de cancelar — sem isso o estoque ficava baixado
        # por um pedido que não existe mais (fix 03/07/2026; a rota web nem
        # permite cancelar depois do envio).
        if novo == 'cancelar' and p.status == 'em_transporte':
            from app.services.pedido_estoque import estornar_industria_pedido
            estornar_industria_pedido(p, user.id, motivo='cancelado via copilot')

        # RECEBER: soma no estoque da loja (qtd conforme pedido, sem divergencia)
        if novo == 'receber':
            from app.services.estoque_helpers import serializar_loja
            serializar_loja(p.loja_id)  # lock por loja antes dos UPDATE
            for item in p.itens:
                qtd = item.quantidade
                item.quantidade_recebida = qtd
                el = EstoqueLoja.query.filter_by(
                    loja_id=p.loja_id,
                    receita_id=item.receita_id,
                    produto_id=item.produto_id,
                    materia_prima_id=item.materia_prima_id,
                ).first()
                if not el:
                    el = EstoqueLoja(loja_id=p.loja_id,
                                     receita_id=item.receita_id,
                                     produto_id=item.produto_id,
                                     materia_prima_id=item.materia_prima_id)
                    db.session.add(el)
                    db.session.flush()
                el.quantidade = (el.quantidade or 0) + qtd
                db.session.add(MovEstoqueLoja(
                    estoque_loja_id=el.id, tipo='entrada_pedido',
                    quantidade=qtd,
                    referencia=f'Pedido #{pid} (copilot)',
                    usuario_id=user.id,
                ))

        p.status = para
        # Carimbo do gesto humano (10/08/2026, auto-pedidos): confirmar/
        # separar/etc. via copilot é decisão de gente — protege o pedido do
        # re-sync do cron (espelho da rota web de confirmar).
        p.modificado_em = agora()
        p.modificado_por_id = user.id
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        logger.exception('mudar_status_pedido %s falhou', novo)
        return {'ok': False, 'erro': f'Erro: {exc}'}

    # O aviso de pedido ENTREGUE nao dispara mais aqui (14/08/2026): o
    # digest das 12:00 (pedidos_notificacao.enviar_digest_recebimentos)
    # acumula e manda UMA mensagem — ver seru_cron.

    resultado = {'ok': True, 'pedido_id': pid, 'novo_status': para,
                 'registro_tipo': 'pedido_loja', 'registro_id': pid,
                 'url': f'/pedidos/{pid}'}
    if aviso_corte:
        resultado['aviso'] = aviso_corte

    # Se acabou de marcar como separado, ja gera o QR Code de saida e
    # devolve no resultado pro Slack mostrar pro motorista escanear.
    if para == 'separado':
        try:
            from datetime import timedelta

            from flask import url_for

            from app.models import PedidoQRCode
            qr = PedidoQRCode(
                token=secrets.token_urlsafe(24),
                pedido_id=pid, tipo='saida',
                criado_por_id=getattr(user, 'id', None),
                expira_em=agora() + timedelta(hours=4),
            )
            db.session.add(qr)
            db.session.commit()
            try:
                resultado['qr_url'] = url_for('handshake.handshake',
                                                token=qr.token, _external=True)
                resultado['qr_png_url'] = url_for('handshake.qr_img',
                                                    token=qr.token, _external=True)
            except RuntimeError:
                # Sem request context (rodando em thread sem app context)
                base = os.environ.get('APP_BASE_URL', '').rstrip('/')
                if base:
                    resultado['qr_url'] = f'{base}/handshake/{qr.token}'
                    resultado['qr_png_url'] = f'{base}/handshake/qr-img/{qr.token}.png'
        except Exception:  # noqa: BLE001
            logger.exception('falha ao gerar QR pos-separacao')
            # Status mudou OK, so o QR falhou — admin pode gerar manual

    return resultado


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
    from app.models import Funcionario
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
        from app.models import Projeto, TarefaProjeto
    except ImportError:
        return {'ok': False, 'erro': 'Modelo TarefaProjeto/Projeto nao existe'}

    projeto = None
    proj_nome = (params.get('projeto_nome') or '').strip()
    if proj_nome:
        from app.models import ProjetoArea

        # Áreas 'igreja'/'vida' são privadas do dono: funcionário/gerente no
        # Slack não pode criar tarefa nelas nem descobrir o nome de projeto
        # privado pelo eco do resultado (mesma regra das rotas web).
        q = Projeto.query.join(ProjetoArea)
        if not (callable(getattr(user, 'is_dono', None)) and user.is_dono()):
            q = q.filter(ProjetoArea.tipo == 'empresa')
        # Match exato (case-insensitive) primeiro; senão substring, preferindo
        # o nome mais curto (evita "a" casar um projeto arbitrário).
        projeto = q.filter(db.func.lower(Projeto.nome) == proj_nome.lower()).first()
        if projeto is None:
            projeto = (q.filter(Projeto.nome.ilike(f'%{proj_nome}%'))
                       .order_by(db.func.length(Projeto.nome)).first())
    if projeto is None:
        # projeto_id e NOT NULL: sem projeto (ou nome que nao casou),
        # a tarefa cai na Inbox (projeto "Avulsas") — mesmo destino do
        # quick-add da tela /projetos.
        from app.blueprints.projetos.routes import _get_inbox_projeto
        projeto = _get_inbox_projeto()

    prazo = None
    aviso = None
    if params.get('data_prazo'):
        try:
            prazo = datetime.strptime(params['data_prazo'], '%Y-%m-%d').date()
        except ValueError:
            # Não silenciar: a tarefa nasce, mas o chamador fica sabendo que
            # o prazo pedido não foi entendido (formato esperado: AAAA-MM-DD).
            aviso = (f'data_prazo "{params["data_prazo"]}" invalida '
                     '(use AAAA-MM-DD) — tarefa criada SEM prazo')

    t = TarefaProjeto(
        nome=titulo,
        projeto_id=projeto.id,
        prazo=prazo,
    )
    db.session.add(t)
    db.session.commit()
    res = {'ok': True, 'tarefa_id': t.id, 'titulo': titulo,
           'projeto': projeto.nome, 'prazo': prazo.isoformat() if prazo else None,
           'registro_tipo': 'tarefa_projeto', 'registro_id': t.id}
    if aviso:
        res['aviso'] = aviso
    return res


# ───── Tools de Planejamento — READ ────────────────────────────────────

def _read_consultar_foco(params, user):
    """Lista projetos foco_12s + tarefas pendentes deles."""
    from app.models import Projeto
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
    q = TarefaProjeto.query.join(Projeto)
    if params.get('apenas_pendentes', True):
        q = q.filter(TarefaProjeto.status.notin_(['feito', 'cancelado']))
    if params.get('apenas_atrasadas'):
        from app.utils import hoje as _hoje_brt
        q = q.filter(TarefaProjeto.prazo.isnot(None), TarefaProjeto.prazo < _hoje_brt())
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


def executar_registrar_desperdicio_lote(params, user):
    """Aplica varios desperdicios de uma loja num so commit.

    Itens sem item resolvido entram em `ignorados`. Quantidade > saldo
    gera mov 'desperdicio_sem_estoque' igual no executor single.
    """
    from app.models import Desperdicio, EstoqueLoja, MovEstoqueLoja

    loja = _resolver_loja_para_user(params.get('loja_id'),
                                     params.get('loja_nome'), user)
    if not loja:
        nome_tentado = params.get('loja_nome') or params.get('loja_id')
        if nome_tentado:
            return {'ok': False, 'erro': f'Loja "{nome_tentado}" nao encontrada.'}
        return {'ok': False, 'erro': 'Especifique a loja.'}

    itens = params.get('itens') or []
    if not itens:
        return {'ok': False, 'erro': 'Lista de itens vazia'}

    from app.services.estoque_helpers import serializar_loja
    serializar_loja(loja.id)  # lock por loja antes das baixas do lote

    # Fonte única das regras (compartilhada com a tela — 03/07/2026).
    from app.services.desperdicio_core import (
        normalizar_motivo,
        reaproveita_sem_baixa,
    )
    motivo = normalizar_motivo(params.get('motivo'))
    obs_lote = (params.get('observacao') or '').strip() or None

    aplicados = []
    ignorados = []

    for item in itens:
        nome = (item.get('nome') or '').strip()
        try:
            qtd = int(item.get('quantidade') or 0)
        except (TypeError, ValueError):
            qtd = 0
        if not nome or qtd <= 0:
            ignorados.append({'nome': nome or '?', 'motivo': 'quantidade invalida'})
            continue

        resolvido = item.get('resolvido')
        if not resolvido or not resolvido.get('id'):
            re_resolve = _resolver_item_qualquer(nome)
            if not re_resolve:
                ignorados.append({'nome': nome, 'motivo': 'item nao encontrado no cadastro'})
                continue
            tipo_item, item_id, nome_ok = re_resolve
        else:
            tipo_item = resolvido['tipo']
            item_id = resolvido['id']
            nome_ok = resolvido.get('nome') or nome

        obs_item = (item.get('observacao') or '').strip() or None
        obs_final = obs_item or obs_lote

        # REAPROVEITAVEL: se motivo='validade'/'nao_vendeu' E item marcado,
        # registra desperdicio mas NAO baixa estoque (regra na fonte única).
        reaproveita = reaproveita_sem_baixa(tipo_item, item_id, motivo)

        # CESTA: se for produto-cesta E nao reaproveita, baixa componentes
        componentes_cesta = []
        if tipo_item == 'produto' and not reaproveita:
            from app.models import Produto
            from app.services.cestas import componentes_de_cesta
            produto_obj = Produto.query.get(item_id)
            componentes_cesta = componentes_de_cesta(produto_obj)

        if reaproveita and not (obs_final or '').strip():
            obs_final = '[reaproveitavel — nao baixou estoque]'
        elif reaproveita:
            obs_final = obs_final + ' [reaproveitavel]'

        desp = Desperdicio(
            loja_id=loja.id,
            receita_id=item_id if tipo_item == 'receita' else None,
            produto_id=item_id if tipo_item == 'produto' else None,
            materia_prima_id=item_id if tipo_item == 'mp' else None,
            quantidade=qtd, motivo=motivo, observacao=obs_final,
            criado_por_id=user.id,
        )
        db.session.add(desp)
        db.session.flush()

        if reaproveita:
            ap = {'nome': nome_ok, 'tipo': tipo_item,
                  'quantidade': qtd, 'reaproveitavel': True}
            # Reaproveitavel COM receita de retorno: converte no estoque da
            # loja (baixa o fresco + credita o retorno — decisao do dono
            # 03/07/2026) e o copilot emenda "quantos voltam pra industria?"
            # (criar_retirada_sobras coleta o RETORNO).
            if tipo_item == 'receita':
                from app.services.desperdicio_core import (
                    converter_sobra_para_retorno,
                )
                conv = converter_sobra_para_retorno(
                    loja.id, item_id, qtd, user.id, desp.id)
                if conv:
                    ap['convertido_retorno'] = conv
                    desp.observacao = (
                        ((obs_final + ' ') if obs_final else '')
                        + f'[convertido em {conv["destino"]}]')
                    ap['retirada_sugerida'] = {
                        'item': nome_ok,
                        'qtd_sobra': qtd,
                        'destino': conv['destino'],
                    }
            aplicados.append(ap)
            continue

        if componentes_cesta:
            for col, comp_id, nome_comp, qtd_por_cesta in componentes_cesta:
                qtd_baixar = int(round(qtd * qtd_por_cesta))
                if qtd_baixar <= 0:
                    continue
                filtro_c = {'loja_id': loja.id, col: comp_id}
                el_c = EstoqueLoja.query.filter_by(**filtro_c).first()
                if not el_c:
                    el_c = EstoqueLoja(**filtro_c, quantidade=0)
                    db.session.add(el_c)
                    db.session.flush()
                saldo_c = el_c.quantidade or 0
                baixa_c = min(qtd_baixar, saldo_c)
                el_c.quantidade = saldo_c - baixa_c
                db.session.add(MovEstoqueLoja(
                    estoque_loja_id=el_c.id, tipo='desperdicio', quantidade=baixa_c,
                    referencia=(f'Desperdicio {motivo} cesta '
                                f'[{produto_obj.nome}] {nome_comp} (copilot lote)'),
                    usuario_id=user.id,
                    desperdicio_id=desp.id,
                ))
            aplicados.append({'nome': nome_ok, 'tipo': 'cesta', 'quantidade': qtd})
            continue  # ja registrou tudo

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

        if baixa > 0:
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=el.id, tipo='desperdicio', quantidade=baixa,
                referencia=f'Desperdicio {motivo}'
                + (f' — {obs_final}' if obs_final else '')
                + ' (copilot lote)',
                usuario_id=user.id,
                desperdicio_id=desp.id,
            ))
        if qtd > baixa:
            falta = qtd - baixa
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=el.id, tipo='desperdicio_sem_estoque',
                quantidade=falta,
                referencia=f'Desperdicio {motivo} — sem estoque ({falta}) (copilot lote)',
                usuario_id=user.id,
                desperdicio_id=desp.id,
            ))

        aplicados.append({
            'nome': nome_ok, 'tipo': tipo_item,
            'quantidade': qtd, 'baixado': baixa, 'saldo_anterior': saldo,
        })

    if not aplicados:
        db.session.rollback()
        return {'ok': False, 'erro': f'Nenhum item aplicado. {len(ignorados)} ignorados.',
                'ignorados': ignorados}

    db.session.commit()
    return {
        'ok': True,
        'loja': loja.nome,
        'motivo': motivo,
        'aplicados': aplicados,
        'ignorados': ignorados,
        'total_aplicados': len(aplicados),
        'total_ignorados': len(ignorados),
        # Quantos registraram SEM baixar estoque (reaproveitaveis) — o Slack
        # avisa em vez de confirmar igual a uma baixa real (03/07/2026).
        'reaproveitados_sem_baixa': sum(
            1 for a in aplicados if a.get('reaproveitavel')),
        'registro_tipo': 'desperdicio_lote',
        'registro_id': None,
        'url': f'/pedidos/desperdicio?loja={loja.id}',
    }


def executar_devolver_industria(params, user):
    """Devolve sobras da loja pra industria — duas pontas via service
    `devolucao.devolver_industria` (baixa a loja + credita o congelado na
    receita de retorno). Itens sem match ou MP entram em `ignorados`."""
    from app.services.devolucao import devolver_industria

    loja = _resolver_loja_para_user(params.get('loja_id'),
                                    params.get('loja_nome'), user)
    if not loja:
        nome_tentado = params.get('loja_nome') or params.get('loja_id')
        if nome_tentado:
            return {'ok': False, 'erro': f'Loja "{nome_tentado}" nao encontrada.'}
        return {'ok': False, 'erro': 'Especifique a loja.'}

    itens_svc = []
    ignorados = []
    for item in (params.get('itens') or []):
        nome = (item.get('nome') or '').strip()
        try:
            qtd = int(item.get('quantidade') or 0)
        except (TypeError, ValueError):
            qtd = 0
        if not nome or qtd <= 0:
            ignorados.append({'nome': nome or '?', 'motivo': 'quantidade invalida'})
            continue
        resolvido = item.get('resolvido')
        if not resolvido or not resolvido.get('id'):
            re_resolve = _resolver_item_qualquer(nome)
            if not re_resolve:
                ignorados.append({'nome': nome,
                                  'motivo': 'item nao encontrado no cadastro'})
                continue
            tipo_item, item_id, _nome_ok = re_resolve
        else:
            tipo_item = resolvido['tipo']
            item_id = resolvido['id']
        if tipo_item == 'mp':
            ignorados.append({'nome': nome,
                              'motivo': 'MP nao pode ser devolvida a industria'})
            continue
        itens_svc.append({'tipo': tipo_item, 'id': item_id, 'qtd': qtd})

    if not itens_svc:
        return {'ok': False,
                'erro': f'Nenhum item devolvido. {len(ignorados)} ignorados.',
                'ignorados': ignorados}

    try:
        r = devolver_industria(loja.id, itens_svc, user.id)
    except ValueError as exc:
        db.session.rollback()
        return {'ok': False, 'erro': str(exc), 'ignorados': ignorados}

    return {
        'ok': True,
        'loja': r['loja'],
        'token': r['token'],
        'itens': r['itens'],
        'avisos': r['avisos'],
        'ignorados': ignorados,
        'total_devolvidos': len(r['itens']),
        'total_ignorados': len(ignorados),
        'registro_tipo': 'devolucao_industria',
        'registro_id': None,
        'url': f'/pedidos/estoque-loja?loja={loja.id}',
    }


def executar_criar_retirada_sobras(params, user):
    """Cria a RetiradaSobra do dia seguinte + QR de coleta.

    FOTO OBRIGATORIA (decisao do dono 02/07/2026): `params['imagens']` vem
    embutida pelo slack_bot — da mensagem atual OU, desde 06/07/2026, da
    ultima foto que o usuario mandou no canal (fallback de 2h: no celular a
    foto vem num balao e a quantidade no outro). Sem nenhuma, recusa com
    instrucao clara. A foto sobe pro Dropbox (comprovante da contagem
    declarada)."""
    import base64
    from datetime import timedelta as _td

    from app.models import RetiradaSobra, RetiradaSobraItem
    from app.services.handshake_qr import gerar_qr_retirada
    from app.utils import hoje

    loja = _resolver_loja_para_user(params.get('loja_id'),
                                    params.get('loja_nome'), user)
    if not loja:
        nome_tentado = params.get('loja_nome') or params.get('loja_id')
        if nome_tentado:
            return {'ok': False, 'erro': f'Loja "{nome_tentado}" nao encontrada.'}
        return {'ok': False, 'erro': 'Especifique a loja.'}

    imgs = params.get('imagens') or []
    blob = None
    mimetype = 'image/jpeg'
    for img in imgs:
        try:
            blob = base64.b64decode(img.get('base64') or '')
            mimetype = img.get('mimetype') or 'image/jpeg'
            break
        except Exception:  # noqa: BLE001
            continue
    if not blob:
        return {'ok': False, 'erro': (
            'Não achei nenhuma foto da sobra — nem nesta mensagem, nem nas '
            'últimas 2 horas do canal. Mande a foto aqui e repita quantos '
            'voltam (pode ser em mensagens separadas que eu junto).')}

    itens_ok = []
    ignorados = []
    for item in (params.get('itens') or []):
        nome = (item.get('nome') or '').strip()
        try:
            qtd = int(item.get('quantidade') or 0)
        except (TypeError, ValueError):
            qtd = 0
        if not nome or qtd <= 0:
            ignorados.append({'nome': nome or '?', 'motivo': 'quantidade invalida'})
            continue
        resolvido = item.get('resolvido')
        if not resolvido or not resolvido.get('id'):
            re_resolve = _resolver_item_qualquer(nome)
            if not re_resolve:
                ignorados.append({'nome': nome,
                                  'motivo': 'item nao encontrado no cadastro'})
                continue
            tipo_item, item_id, _n = re_resolve
        else:
            tipo_item, item_id = resolvido['tipo'], resolvido['id']
        if tipo_item == 'mp':
            ignorados.append({'nome': nome, 'motivo': 'MP nao vai pra retirada'})
            continue
        # Receita com retorno configurado: a retirada carrega (e a coleta
        # baixa) a receita de RETORNO — o registro da sobra ja converteu o
        # estoque da loja pra ela (desperdicio_core.converter_sobra_para_
        # retorno) e a industria credita o retorno no recebimento.
        if tipo_item == 'receita':
            _rec = db.session.get(Receita, item_id)
            if _rec is not None and _rec.retorno_receita_id:
                item_id = _rec.retorno_receita_id
        itens_ok.append((tipo_item, item_id, qtd))
    if not itens_ok:
        return {'ok': False,
                'erro': f'Nenhum item válido. {len(ignorados)} ignorados.',
                'ignorados': ignorados}

    data_ret = hoje() + _td(days=1)
    # Foto ANTES do registro (mesma ordem do Contas a Pagar: nao perde o
    # comprovante se o resto falhar). Dropbox indisponivel = erro visivel.
    from app.services.dropbox_storage import upload_publico
    ext = 'png' if 'png' in mimetype else 'jpg'
    up = upload_publico(
        blob, f'/retiradas/{data_ret.isoformat()}-loja{loja.id}.{ext}')

    try:
        ret = RetiradaSobra(
            loja_id=loja.id, data_retirada=data_ret,
            criado_por_id=user.id, foto_url=up['url'],
            foto_storage_path=up.get('storage_path'),
            observacao=(params.get('observacao') or '').strip() or None)
        db.session.add(ret)
        db.session.flush()
        for tipo_item, item_id, qtd in itens_ok:
            db.session.add(RetiradaSobraItem(
                retirada_id=ret.id,
                receita_id=item_id if tipo_item == 'receita' else None,
                produto_id=item_id if tipo_item == 'produto' else None,
                quantidade=qtd))
        qr = gerar_qr_retirada(ret, 'coleta', criado_por_id=user.id)
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        logger.exception('criar_retirada_sobras falhou')
        return {'ok': False, 'erro': f'Erro ao criar a retirada: {exc}'}

    resultado = {
        'ok': True,
        'retirada_id': ret.id,
        'loja': loja.nome,
        'data_retirada': data_ret.isoformat(),
        'itens': [{'tipo': t, 'id': i, 'qtd': q} for t, i, q in itens_ok],
        'ignorados': ignorados,
        'registro_tipo': 'retirada_sobra',
        'registro_id': ret.id,
        'qr_texto': (f':qrcode: *Retirada #{ret.id} criada pra '
                     f'{data_ret.strftime("%d/%m")}.* O motorista escaneia o '
                     'QR abaixo na loja + digita o PIN dele — isso baixa o '
                     'estoque da loja e inicia o transporte.'),
    }
    try:
        from flask import url_for
        resultado['qr_url'] = url_for('handshake.handshake_retirada',
                                      token=qr.token, _external=True)
        resultado['qr_png_url'] = url_for('handshake.qr_img_retirada',
                                          token=qr.token, _external=True)
    except RuntimeError:
        base = os.environ.get('APP_BASE_URL', '').rstrip('/')
        if base:
            resultado['qr_url'] = f'{base}/handshake/r/{qr.token}'
            resultado['qr_png_url'] = f'{base}/handshake/qr-img/r/{qr.token}.png'
    return resultado


def executar_criar_cliente_b2b(params, user):
    """Cadastra novo ClienteB2B. Idempotente por nome — se ja existir,
    retorna o existente sem erro."""
    from app.models import ClienteB2B
    nome = (params.get('nome') or '').strip()
    if not nome:
        return {'ok': False, 'erro': 'Nome obrigatorio.'}
    existente = ClienteB2B.query.filter_by(nome=nome).first()
    if existente:
        return {'ok': True, 'cliente_id': existente.id, 'nome': existente.nome,
                'duplicado': True,
                'registro_tipo': 'cliente_b2b', 'registro_id': existente.id,
                'url': '/b2b/clientes'}
    try:
        desc = float(params.get('desconto_percentual') or 0)
    except (TypeError, ValueError):
        desc = 0
    c = ClienteB2B(
        nome=nome,
        cnpj_cpf=(params.get('cnpj_cpf') or '').strip() or None,
        telefone=(params.get('telefone') or '').strip() or None,
        email=(params.get('email') or '').strip() or None,
        endereco=(params.get('endereco') or '').strip() or None,
        contato=(params.get('contato') or '').strip() or None,
        desconto_percentual=desc,
        observacao=(params.get('observacao') or '').strip() or None,
    )
    db.session.add(c)
    db.session.commit()
    return {'ok': True, 'cliente_id': c.id, 'nome': c.nome,
            'registro_tipo': 'cliente_b2b', 'registro_id': c.id,
            'url': '/b2b/clientes'}


def executar_criar_venda_b2b(params, user):
    """Cria venda B2B + itens + parcelas + baixa estoque do freezer.

    Itens chegam ja resolvidos (ou re-resolve se vier sem). Sem fallback
    silencioso: item nao resolvido aborta com erro claro.
    """
    from app.services import vendas_b2b as svc

    cliente_id = params.get('cliente_id')
    cliente_nome = params.get('cliente_nome') or params.get('cliente_nome_resolvido')
    cliente_avulso = params.get('cliente_avulso', cliente_id is None)
    if not cliente_id and not (cliente_nome or '').strip():
        return {'ok': False, 'erro': 'cliente obrigatorio.'}
    # Se a Claude marcou avulso, manda so cliente_nome (vai como avulso)
    if cliente_avulso:
        cliente_id = None

    data_str = (params.get('data_venda') or '').strip()
    try:
        data_venda = date.fromisoformat(data_str) if data_str else None
    except ValueError:
        data_venda = None
    # Data de entrega = obrigatoria (vai pra fila do padeiro), igual ao formulario.
    data_ent_str = (params.get('data_entrega') or '').strip()
    try:
        data_entrega = date.fromisoformat(data_ent_str) if data_ent_str else None
    except ValueError:
        data_entrega = None
    if not data_entrega:
        return {'ok': False,
                'erro': 'Informe a data de entrega (dia que vai pro padeiro).'}

    itens_in = params.get('itens') or []
    itens_payload = []
    nao_resolvidos = []
    for it in itens_in:
        if it.get('erro'):
            nao_resolvidos.append(it.get('nome_original') or it.get('nome') or '?')
            continue
        est = it.get('estado')  # enriquecido ja traz; senao parseia do nome
        resolvido = it.get('resolvido')
        if not resolvido or not resolvido.get('id'):
            # Re-resolve pelo nome (caso venha do Claude direto), separando estado.
            nome_raw = (it.get('nome_original') or it.get('nome') or '').strip()
            nome, est_nome = _separar_estado(nome_raw)
            if est is None:
                est = est_nome
            matches = _resolver_produto(nome) if nome else []
            if not matches:
                nao_resolvidos.append(nome_raw or '?')
                continue
            resolvido = matches[0]
        if est not in (None, 'backup', 'assado'):
            est = None
        try:
            qtd = int(it.get('quantidade') or 0)
        except (TypeError, ValueError):
            qtd = 0
        if qtd <= 0:
            continue
        itens_payload.append({
            'tipo': resolvido['tipo'],
            'id': resolvido['id'],
            'quantidade': qtd,
            'preco_unitario': float(it.get('preco_unitario') or 0),
            'desconto_percentual': float(it.get('desconto_percentual') or 0),
            'estado': est,
        })

    if not itens_payload:
        return {'ok': False,
                'erro': f'Nenhum item valido. Nao achei: {", ".join(nao_resolvidos) or "—"}'}

    parcelas_in = params.get('parcelas') or []
    parcelas_payload = []
    for p in parcelas_in:
        venc = p.get('vencimento')
        try:
            valor = float(p.get('valor') or 0)
        except (TypeError, ValueError):
            valor = 0
        if not venc or valor <= 0:
            continue
        parcelas_payload.append({
            'vencimento': venc,
            'valor': valor,
            'forma_pagamento': (p.get('forma_pagamento') or '').strip() or None,
        })

    try:
        frete_valor = float(params.get('frete_valor') or 0)
    except (TypeError, ValueError):
        frete_valor = 0

    try:
        venda = svc.criar_venda(
            cliente_id=cliente_id,
            cliente_nome=cliente_nome if not cliente_id else None,
            data_venda=data_venda,
            data_entrega=data_entrega,
            itens=itens_payload,
            parcelas=parcelas_payload or None,
            observacao=(params.get('observacao') or '').strip() or None,
            nf_numero=(params.get('nf_numero') or '').strip() or None,
            frete_valor=frete_valor,
            user=user,
        )
    except ValueError as exc:
        db.session.rollback()
        return {'ok': False, 'erro': str(exc)}

    return {
        'ok': True,
        'venda_id': venda.id,
        'cliente': venda.cliente_display,
        'valor_total': venda.valor_total,
        'frete_valor': venda.frete_valor,
        'itens_salvos': len(venda.itens),
        'parcelas': len(venda.parcelas),
        'nao_resolvidos': nao_resolvidos,
        'registro_tipo': 'venda_b2b',
        'registro_id': venda.id,
        'url': f'/b2b/vendas/{venda.id}',
    }


def executar_anexar_foto_pedido(params, user):
    """Salva 1+ fotos como FotoRecebimento no pedido.

    `params['imagens']` = lista de {mimetype, base64} embutida pelo slack_bot
    quando o usuario anexou imagens na msg que originou a acao.
    """
    import base64

    from app.models import FotoRecebimento, PedidoLoja

    pid = params.get('pedido_id')
    p = PedidoLoja.query.get(pid)
    if not p:
        return {'ok': False, 'erro': f'Pedido #{pid} nao encontrado'}

    imgs = params.get('imagens') or []
    if not imgs:
        return {'ok': False, 'erro': 'Nenhuma imagem anexada na mensagem.'}

    salvas = 0
    try:
        for img in imgs:
            b64 = img.get('base64')
            if not b64:
                continue
            try:
                blob = base64.b64decode(b64)
            except Exception:
                continue
            db.session.add(FotoRecebimento(
                pedido_id=p.id,
                imagem=blob,
                mimetype=img.get('mimetype') or 'image/jpeg',
                enviada_por=user.id,
            ))
            salvas += 1
        db.session.commit()
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        logger.exception('anexar_foto_pedido falhou')
        return {'ok': False, 'erro': f'Erro: {exc}'}

    if salvas == 0:
        return {'ok': False, 'erro': 'Nao consegui decodificar nenhuma imagem.'}

    return {'ok': True, 'pedido_id': pid, 'fotos_salvas': salvas,
            'registro_tipo': 'pedido_loja', 'registro_id': pid,
            'url': f'/pedidos/{pid}'}


# ── Desperdicio (sobra do dia / vencido) ───────────────────────────────

def _resolver_item_qualquer(nome):
    """Match flex: tenta Receita, Produto e MP. Retorna (tipo, id, nome) ou None.

    Tipo retornado: 'receita' | 'produto' | 'mp'.
    """
    from sqlalchemy import func
    nome = (nome or '').strip()
    if not nome:
        return None
    r = Receita.query.filter(func.lower(Receita.nome) == nome.lower(),
                             Receita.arquivada_em.is_(None)).first()
    if r:
        return ('receita', r.id, r.nome)
    # Produto SEM filtro de ativo DE PROPOSITO (pos-revisao 19/07/2026):
    # este resolver serve desperdicio/devolucao/retirada — operacoes sobre
    # estoque FISICO ja existente. Produto soft-deletado com saldo
    # remanescente precisa continuar escoavel pelo bot (a criacao de pedido/
    # venda nova usa _resolver_produto, esse sim filtrado).
    p = Produto.query.filter(func.lower(Produto.nome) == nome.lower()).first()
    if p:
        return ('produto', p.id, p.nome)
    m = MateriaPrima.ativas().filter(func.lower(MateriaPrima.nome) == nome.lower()).first()
    if m:
        return ('mp', m.id, m.nome)
    # Fuzzy: coleta top 10 de cada e desempata por proximidade.
    cands = []
    for r in (Receita.query.filter(Receita.nome.ilike(f'%{nome}%'),
                                   Receita.arquivada_em.is_(None)).limit(10).all()):
        cands.append(('receita', r.id, r.nome))
    for p in Produto.query.filter(Produto.nome.ilike(f'%{nome}%')).limit(10).all():
        cands.append(('produto', p.id, p.nome))
    for m in MateriaPrima.ativas().filter(MateriaPrima.nome.ilike(f'%{nome}%')).limit(10).all():
        cands.append(('mp', m.id, m.nome))
    if cands:
        cands.sort(key=lambda c: _score_proximidade(nome, c[2]))
        return cands[0]
    return None


def _resolver_loja_para_user(loja_id, loja_nome, user):
    """Resolve loja por id ou nome (fuzzy).

    NUNCA cai pra user.loja_id como fallback silencioso — usuario nao tem
    loja "responsavel". Se nada for especificado, retorna None pro caller
    pedir explicitamente.
    """
    if loja_id:
        l = Loja.query.get(int(loja_id))
        if l:
            return l
    if loja_nome:
        from app.utils import resolver_loja_por_nome
        l = resolver_loja_por_nome(loja_nome)
        if l:
            return l
    return None


def executar_registrar_desperdicio(params, user):
    from app.models import Desperdicio, EstoqueLoja, MovEstoqueLoja
    loja = _resolver_loja_para_user(params.get('loja_id'),
                                     params.get('loja_nome'), user)
    if not loja:
        nome_tentado = params.get('loja_nome') or params.get('loja_id')
        if nome_tentado:
            return {'ok': False, 'erro': f'Loja "{nome_tentado}" nao encontrada. Verifique o nome.'}
        return {'ok': False, 'erro': 'Especifique a loja (ex: Anesio, Nebraska, Ribeiro do Vale).'}

    from app.services.estoque_helpers import serializar_loja
    serializar_loja(loja.id)  # lock por loja antes da baixa do desperdicio

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

    # Regras compartilhadas com a tela /pedidos/desperdicio (03/07/2026):
    # motivo canônico + decisão de reaproveitável na MESMA fonte, pra os dois
    # canais nunca divergirem de novo.
    from app.services.desperdicio_core import (
        normalizar_motivo,
        reaproveita_sem_baixa,
    )
    motivo = normalizar_motivo(params.get('motivo'))
    observacao = (params.get('observacao') or '').strip() or None

    # REAPROVEITAVEL: registra Desperdicio (pra historico) mas NAO baixa
    # estoque — o item vai virar outra coisa (croissant→almond).
    reaproveita = reaproveita_sem_baixa(tipo_item, item_id, motivo)

    # CESTA: se for produto-cesta, baixa componentes
    componentes_cesta = []
    if tipo_item == 'produto' and not reaproveita:
        from app.models import Produto
        from app.services.cestas import componentes_de_cesta
        produto_obj = Produto.query.get(item_id)
        componentes_cesta = componentes_de_cesta(produto_obj)

    desp = Desperdicio(
        loja_id=loja.id,
        receita_id=item_id if tipo_item == 'receita' else None,
        produto_id=item_id if tipo_item == 'produto' else None,
        materia_prima_id=item_id if tipo_item == 'mp' else None,
        quantidade=qtd, motivo=motivo, observacao=observacao,
        criado_por_id=user.id,
    )
    db.session.add(desp)
    db.session.flush()  # id pro vinculo dos movimentos (estorno exato)

    conv = None
    if reaproveita:
        # Reaproveitavel COM receita de retorno: converte no estoque da loja
        # (baixa o fresco + credita o retorno — decisao do dono 03/07/2026).
        # Sem retorno configurado: registro sem movimento, como antes.
        baixa = 0
        if tipo_item == 'receita':
            from app.services.desperdicio_core import (
                converter_sobra_para_retorno,
            )
            conv = converter_sobra_para_retorno(
                loja.id, item_id, qtd, user.id, desp.id)
        sufixo = (f'[convertido em {conv["destino"]}]' if conv
                  else '[reaproveitavel — nao baixou estoque]')
        if not (desp.observacao or '').strip():
            desp.observacao = sufixo
        else:
            desp.observacao = desp.observacao + ' ' + sufixo
    elif componentes_cesta:
        # Loja so estoca componentes; desconta cada um
        for col, comp_id, nome_comp, qtd_por_cesta in componentes_cesta:
            qtd_baixar = int(round(qtd * qtd_por_cesta))
            if qtd_baixar <= 0:
                continue
            filtro_c = {'loja_id': loja.id, col: comp_id}
            el_c = EstoqueLoja.query.filter_by(**filtro_c).first()
            if not el_c:
                el_c = EstoqueLoja(**filtro_c, quantidade=0)
                db.session.add(el_c)
                db.session.flush()
            saldo_c = el_c.quantidade or 0
            baixa_c = min(qtd_baixar, saldo_c)
            el_c.quantidade = saldo_c - baixa_c
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=el_c.id, tipo='desperdicio', quantidade=baixa_c,
                referencia=(f'Desperdicio {motivo} cesta [{produto_obj.nome}] '
                            f'{nome_comp} (copilot)'),
                usuario_id=user.id,
                desperdicio_id=desp.id,
            ))
        baixa = 0  # nao tem baixa "cabeca", ja foi nos componentes
    else:
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

        if baixa > 0:
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=el.id, tipo='desperdicio', quantidade=baixa,
                referencia=f'Desperdicio {motivo}'
                + (f' — {observacao}' if observacao else '')
                + ' (copilot)',
                usuario_id=user.id,
                desperdicio_id=desp.id,
            ))
        if qtd > baixa:
            falta = qtd - baixa
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=el.id, tipo='desperdicio_sem_estoque',
                quantidade=falta,
                referencia=f'Desperdicio {motivo} — registrado sem estoque ({falta}) (copilot)',
                usuario_id=user.id,
                desperdicio_id=desp.id,
            ))

    db.session.commit()
    out = {'ok': True, 'desperdicio_id': desp.id,
           'loja': loja.nome, 'item': nome_item_ok,
           'quantidade': qtd, 'baixado_do_estoque': baixa,
           'motivo': motivo,
           # Sinaliza pro canal (Slack) avisar que o estoque NAO baixou —
           # antes a confirmacao era identica com ou sem baixa (03/07/2026).
           'reaproveitavel_sem_baixa': bool(reaproveita),
           # Conversao fresco -> receita de retorno no estoque da loja
           # (None quando o item nao tem retorno configurado).
           'convertido_retorno': conv,
           'registro_tipo': 'desperdicio', 'registro_id': desp.id,
           'url': f'/pedidos/desperdicio?loja={loja.id}'}
    # Reaproveitavel COM receita de retorno -> o copilot emenda "quantos
    # voltam pra industria?" e cria a retirada (criar_retirada_sobras).
    if reaproveita and tipo_item == 'receita':
        from app.models import Receita as _R
        _rec = _R.query.get(item_id)
        if _rec is not None and _rec.retorno_receita_id:
            out['retirada_sugerida'] = {
                'item': nome_item_ok, 'qtd_sobra': qtd,
                'destino': (_rec.retorno_receita.nome
                            if _rec.retorno_receita else nome_item_ok),
            }
    return out


def _read_consultar_catalogo_site(params, user):
    """Reusa bot_tools.consultar_produtos (chatbot de cliente) — ele ja
    bate na API do VNDA, normaliza nome/sku/preco e enriquece com a URL
    da pagina (montada do slug). Pra WhatsApp/Slack formatamos uma
    mensagem curta: top 3 com link clicavel; descricao truncada."""
    from app.services import bot_tools
    busca = (params or {}).get('busca') or ''
    res = bot_tools.consultar_produtos(busca)
    if 'erro' in res:
        return {'texto': f'Catalogo indisponivel: {res["erro"]}'}
    produtos = res.get('produtos') or []
    if not produtos:
        return {'texto': f'Nada no catalogo do site bateu com "{busca}".'}
    linhas = [f'*Catalogo do site — "{busca}":*', '']
    for p in produtos[:5]:
        disp = '' if p.get('disponivel') else ' _(esgotado hoje)_'
        preco = p.get('preco')
        preco_txt = f' — R$ {preco}' if preco is not None else ''
        linhas.append(f'• *{p.get("nome")}*{preco_txt}{disp}')
        if p.get('url'):
            linhas.append(f'  {p["url"]}')
    if len(produtos) > 5:
        linhas.append(f'_(+ {len(produtos) - 5} resultados — refina o termo se precisar)_')
    return {'texto': '\n'.join(linhas), 'total': len(produtos)}


def _read_consultar_cartinhas(params, user):
    """Cartinhas cadastradas/editadas nas ultimas N horas (default 48h).
    A tabela tem 1 linha por pedido (texto sobrescrito a cada edicao) —
    `atualizado_em` captura a ultima escrita. Fonte: CartinhaEntrega."""
    from app.models import CartinhaEntrega, Usuario
    try:
        dias = int(params.get('dias') or 2)
    except (TypeError, ValueError):
        dias = 2
    dias = max(1, min(30, dias))
    desde = agora() - timedelta(days=dias)
    cartinhas = (CartinhaEntrega.query
                 .filter(CartinhaEntrega.atualizado_em >= desde)
                 .order_by(CartinhaEntrega.atualizado_em.desc())
                 .limit(50).all())
    if not cartinhas:
        return {'texto': f'Nenhuma cartinha cadastrada nos ultimos {dias} dia(s).'}
    autores = {u.id: u.nome for u in Usuario.query.filter(
        Usuario.id.in_({c.atualizado_por for c in cartinhas if c.atualizado_por})).all()}
    linhas = [f'*{len(cartinhas)} cartinha(s) nos ultimos {dias} dia(s):*', '']
    for c in cartinhas[:20]:
        quem = autores.get(c.atualizado_por, '—')
        quando = c.atualizado_em.strftime('%d/%m %H:%M') if c.atualizado_em else '—'
        preview = (c.texto or '').strip().replace('\n', ' / ')[:120]
        linhas.append(f'• `{c.pedido_code}` — _{preview}_')
        linhas.append(f'   ↳ {quem} em {quando}')
    if len(cartinhas) > 20:
        linhas.append(f'_(+ {len(cartinhas) - 20} mais — abra /audit pra ver tudo)_')
    return {'texto': '\n'.join(linhas), 'total': len(cartinhas)}


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


def _read_consultar_vigia(params, user):
    """Le VigiaVeredito — onde a vigia do chatbot persiste tudo que viu
    (reclamacoes, handoff preguicoso, 'bot delirou'). Permite o dono pedir
    detalhes do alerta que recebeu via WhatsApp."""
    from app.models import VigiaVeredito

    conv_id = (params.get('conv_id') or '').strip() or None
    palavra = (params.get('palavra') or '').strip() or None

    q = VigiaVeredito.query
    if conv_id:
        q = q.filter(VigiaVeredito.conv_id == conv_id)
    else:
        try:
            dias = int(params.get('dias') or 1)
        except (TypeError, ValueError):
            dias = 1
        dias = max(1, min(14, dias))
        desde = agora() - timedelta(days=dias)
        q = q.filter(VigiaVeredito.criado_em >= desde)
        # Default = alta. None/'' explicito = todas. 'media' = so media.
        if 'gravidade' in params:
            grav = params.get('gravidade')
            if grav:  # 'alta' ou 'media'
                q = q.filter(VigiaVeredito.gravidade == grav)
        else:
            q = q.filter(VigiaVeredito.gravidade == 'alta')

    if palavra:
        like = f'%{palavra.lower()}%'
        from sqlalchemy import func, or_
        q = q.filter(or_(
            func.lower(VigiaVeredito.mensagem_cliente).like(like),
            func.lower(VigiaVeredito.motivo_vigia).like(like),
            func.lower(VigiaVeredito.cliente).like(like),
        ))

    regs = q.order_by(VigiaVeredito.criado_em.desc()).limit(40).all()
    if not regs:
        return {'texto': 'Vigia: nenhum veredito bate com esse filtro.'}

    base_cw = (current_app.config.get('CHATWOOT_URL') or '').rstrip('/')
    acc = (current_app.config.get('CHATWOOT_ACCOUNT_ID') or '').strip()

    def _link(cid):
        if base_cw and acc and cid:
            return f'{base_cw}/app/accounts/{acc}/conversations/{cid}'
        return ''

    linhas = [f'**Vigia ({len(regs)} verediltos):**', '']
    for r in regs:
        quando = r.criado_em.strftime('%d/%m %H:%M') if r.criado_em else '?'
        grav = (r.gravidade or '–').upper()
        cliente = r.cliente or '(sem nome)'
        cid_txt = f' · conv #{r.conv_id}' if r.conv_id else ''
        alerta = ' ⚠️' if r.alerta else ''
        linhas.append(f'**{quando} · {grav}{alerta} · {cliente}{cid_txt}**')
        if r.mensagem_cliente:
            msg = r.mensagem_cliente[:280].replace('\n', ' ')
            linhas.append(f'  Cliente: "{msg}"')
        if r.bot_acao or r.bot_motivo:
            acao = r.bot_acao or '?'
            motivo = (r.bot_motivo or '').replace('\n', ' ')[:200]
            sep = ' — ' if motivo else ''
            linhas.append(f'  Bot: {acao}{sep}{motivo}')
        if r.motivo_vigia:
            mv = r.motivo_vigia[:280].replace('\n', ' ')
            linhas.append(f'  Vigia: {mv}')
        link = _link(r.conv_id)
        if link:
            linhas.append(f'  {link}')
        linhas.append('')
    return {'texto': '\n'.join(linhas).rstrip()}


def _read_consultar_conversa_chatwoot(params, user):
    """Le o historico real de uma conversa especifica no Chatwoot."""
    from app.services import chatwoot

    conv_id = (params.get('conv_id') or '').strip()
    if not conv_id:
        return {'texto': 'Erro: conv_id e obrigatorio.'}
    try:
        limite = int(params.get('limite') or 20)
    except (TypeError, ValueError):
        limite = 20
    limite = max(1, min(50, limite))

    historico = chatwoot.buscar_historico(conv_id, limite=limite)
    if not historico:
        return {'texto': (f'Conversa #{conv_id}: sem mensagens (ou Chatwoot '
                          'nao configurado / token sem acesso).')}

    base_cw = (current_app.config.get('CHATWOOT_URL') or '').rstrip('/')
    acc = (current_app.config.get('CHATWOOT_ACCOUNT_ID') or '').strip()
    link = (f'{base_cw}/app/accounts/{acc}/conversations/{conv_id}'
            if base_cw and acc else '')

    linhas = [f'**Conversa #{conv_id} ({len(historico)} msgs):**']
    if link:
        linhas.append(link)
    linhas.append('')
    for m in historico:
        role = m.get('role') or '?'
        quem = 'Cliente' if role == 'user' else 'Bot/Atendente'
        content = (m.get('content') or '').replace('\n', ' ')[:500]
        imgs = m.get('imagens') or []
        prefixo_img = f' [+{len(imgs)} imagem(s)]' if imgs else ''
        linhas.append(f'- **{quem}**: {content}{prefixo_img}')
    return {'texto': '\n'.join(linhas)}


def _read_listar_conversas_chatwoot(params, user):
    """Lista conversas paradas no Chatwoot — fila do bot (pending) ou
    clientes esperando atendente humano (open)."""
    from app.services import chatwoot

    status = (params.get('status') or 'open').strip().lower()
    if status not in ('pending', 'open'):
        status = 'open'
    try:
        min_min = int(params.get('min_minutos') or 0)
    except (TypeError, ValueError):
        min_min = 0
    min_min = max(0, min(1440, min_min))

    paradas = chatwoot.listar_conversas_paradas(
        min_minutos=min_min, status=status, limite=50)
    if not paradas:
        rotulo = ('na fila do bot' if status == 'pending'
                  else 'esperando atendente')
        return {'texto': f'Nenhuma conversa {rotulo}'
                         + (f' ha mais de {min_min} min' if min_min else '')
                         + '.'}

    base_cw = (current_app.config.get('CHATWOOT_URL') or '').rstrip('/')
    acc = (current_app.config.get('CHATWOOT_ACCOUNT_ID') or '').strip()
    rotulo = ('Fila do bot (pending)' if status == 'pending'
              else 'Esperando atendente (open)')
    linhas = [f'**{rotulo} — {len(paradas)} conversa(s):**', '']
    for c in paradas:
        cid = c.get('id')
        nome = c.get('nome_contato') or '(sem nome)'
        mins = c.get('minutos_paradas', 0)
        link = (f'{base_cw}/app/accounts/{acc}/conversations/{cid}'
                if base_cw and acc and cid else '')
        sufixo = f' · {link}' if link else ''
        linhas.append(f'- conv #{cid} · {nome} · parada ha {mins}min{sufixo}')
    return {'texto': '\n'.join(linhas)}


def _read_consultar_cliente_b2b(params, user):
    from app.models import ClienteB2B
    q = ClienteB2B.query.filter_by(ativo=True)
    nome = (params.get('nome') or '').strip()
    if nome:
        q = q.filter(ClienteB2B.nome.ilike(f'%{nome}%'))
    clientes = q.order_by(ClienteB2B.nome).limit(20).all()
    if not clientes:
        return {'texto': 'Nenhum cliente B2B encontrado' + (f' com "{nome}"' if nome else '.')}
    linhas = []
    for c in clientes:
        partes = [c.nome]
        if c.cnpj_cpf:
            partes.append(c.cnpj_cpf)
        if c.contato:
            partes.append(f'contato: {c.contato}')
        if c.telefone:
            partes.append(c.telefone)
        if c.desconto_percentual:
            partes.append(f'{c.desconto_percentual:.0f}% desc')
        linhas.append('• ' + ' · '.join(partes))
    return {'texto': '\n'.join(linhas)}


def _read_consultar_notas(params, user):
    """Busca nas notas persistentes. Vazio = mais recentes."""
    from app.services import notas as notas_svc
    termo = (params.get('termo') or '').strip()
    encontradas = notas_svc.buscar(termo)
    if not encontradas:
        return {'texto': '_(nenhuma nota encontrada — registre com '
                          'registrar_nota se quiser ensinar algo novo)_'}
    return {'texto': notas_svc.serializar_pro_agente(encontradas)}


def _read_registrar_nota(params, user):
    """Cria nota nova. Executa direto (sem aprovacao Block Kit) — anotacao
    e leve e o admin sempre pode arquivar em /notas se virou ruido.

    Origem vem de `_canal` (injetado em `interpretar`): 'whatsapp' →
    `copilot_wpp`, 'slack' (default) → `copilot_slack`. Permite filtrar
    em /notas e auditar de onde veio o ensinamento."""
    from app.services import notas as notas_svc
    titulo = (params.get('titulo') or '').strip()
    conteudo = (params.get('conteudo') or '').strip()
    if not titulo or not conteudo:
        return {'erro': 'titulo e conteudo sao obrigatorios'}
    tags = params.get('tags') or ''
    canal = (params.get('_canal') or 'slack').lower()
    origem = 'copilot_wpp' if canal in ('whatsapp', 'wpp') else 'copilot_slack'
    try:
        n = notas_svc.registrar(
            titulo, conteudo, tags=tags, origem=origem,
            criada_por_id=getattr(user, 'id', None))
    except ValueError as e:
        return {'erro': str(e)}
    return {'texto': f'✅ Nota #{n.id} registrada: *{n.titulo}*\n'
            f'_(consultavel via "/notas" ou perguntando pro copilot)_'}


def _read_enviar_digest_whatsapp(params, user):
    """Envia WhatsApp pro ZAPI_NUMERO_DESTINO. Se texto_custom, manda esse;
    senao, monta o digest de tarefas."""
    from app.services import zapi as zapi_svc
    from app.services import zapi_resumos

    if not zapi_svc.disponivel():
        return {'texto': ':warning: Z-API nao configurado. Defina ZAPI_INSTANCE_ID + ZAPI_TOKEN no Railway.'}

    numero = (current_app.config.get('ZAPI_NUMERO_DESTINO') or '').strip()
    if not numero:
        return {'texto': ':warning: ZAPI_NUMERO_DESTINO nao configurado.'}

    texto_custom = (params.get('texto_custom') or '').strip()
    if texto_custom:
        texto = texto_custom
        rotulo = 'mensagem'
    else:
        texto = zapi_resumos.montar_digest_tarefas(user)
        rotulo = 'digest de tarefas'

    res = zapi_svc.enviar_texto(numero, texto)
    if res.get('ok'):
        return {'texto': f':white_check_mark: {rotulo.capitalize()} enviada pra {numero}.\n\n_Preview:_\n```\n{texto[:800]}\n```'}
    return {'texto': f':warning: Falha ao enviar: {res.get("erro", "?")}'}


# ─── Dispatch tables ──────────────────────────────────────────────────
# Roteamento unico de todas as tools. Adicionar nova tool = 1 linha aqui.

_READ_HANDLERS = {
    'consultar_pedido': _read_consultar_pedido,
    'consultar_estoque': _read_consultar_estoque,
    'consultar_fornecedores': _read_consultar_fornecedores,
    'consultar_margem': _read_consultar_margem,
    'consultar_funcionario': _read_consultar_funcionario,
    'consultar_caixa': _read_consultar_caixa,
    'consultar_vendas_itens': _read_consultar_vendas_itens,
    'prever_pedido': _read_prever_pedido,
    'consultar_foco': _read_consultar_foco,
    'consultar_tarefas': _read_consultar_tarefas,
    'consultar_desperdicio': _read_consultar_desperdicio,
    'consultar_catalogo_site': _read_consultar_catalogo_site,
    'consultar_cartinhas': _read_consultar_cartinhas,
    'consultar_vigia': _read_consultar_vigia,
    'consultar_conversa_chatwoot': _read_consultar_conversa_chatwoot,
    'listar_conversas_chatwoot': _read_listar_conversas_chatwoot,
    'enviar_digest_whatsapp': _read_enviar_digest_whatsapp,
    'consultar_cliente_b2b': _read_consultar_cliente_b2b,
    # Memoria persistente: 'registrar' eh write mas executa sem aprovacao
    # Block Kit (atrito demais pra anotar). Errou? admin arquiva em /notas.
    'consultar_notas': _read_consultar_notas,
    'registrar_nota': _read_registrar_nota,
}

_EXEC_HANDLERS = {
    'criar_pedido': executar_criar_pedido,
    'editar_pedido': executar_editar_pedido,
    'receber_mp': executar_receber_mp,
    'ajuste_estoque': executar_ajuste_estoque,
    'mudar_status_pedido': executar_mudar_status_pedido,
    'criar_fornecedor': executar_criar_fornecedor,
    'marcar_ponto': executar_marcar_ponto,
    'criar_tarefa': executar_criar_tarefa,
    'marcar_tarefa_feita': executar_marcar_tarefa_feita,
    'balanco_congelados': executar_balanco_congelados,
    'entrada_lote_loja': executar_entrada_lote_loja,
    'devolver_industria': executar_devolver_industria,
    'criar_retirada_sobras': executar_criar_retirada_sobras,
    'registrar_desperdicio': executar_registrar_desperdicio,
    'registrar_desperdicio_lote': executar_registrar_desperdicio_lote,
    'criar_venda_b2b': executar_criar_venda_b2b,
    'criar_cliente_b2b': executar_criar_cliente_b2b,
    'anexar_foto_pedido': executar_anexar_foto_pedido,
}


def _executar_read(tool_name, params, user):
    """Dispatch unico das tools de leitura."""
    handler = _READ_HANDLERS.get(tool_name)
    if not handler:
        return {'erro': f'tool de leitura desconhecida: {tool_name}'}
    try:
        return handler(params, user)
    except Exception as exc:  # noqa: BLE001
        logger.exception('Copilot read tool %s falhou', tool_name)
        return {'erro': str(exc)}


def executar(tipo_acao, params, user):
    """Dispatch unico das tools de escrita. Chamado apos aprovacao."""
    # receber_pedido reusa o executor de mudar_status_pedido
    if tipo_acao == 'receber_pedido':
        return executar_mudar_status_pedido(
            {**params, 'novo_status': 'receber'}, user)
    handler = _EXEC_HANDLERS.get(tipo_acao)
    if not handler:
        return {'ok': False, 'erro': f'tipo de acao desconhecido: {tipo_acao}'}
    return handler(params, user)

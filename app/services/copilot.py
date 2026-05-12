"""Servico do Copilot: interpreta comandos em linguagem natural via
Claude Haiku 4.5 e retorna acoes estruturadas pra preview/aprovacao.

Atualmente suporta uma tool: criar_pedido. Pre-validacao client-side
NUNCA executa diretamente — sempre retorna preview pro usuario confirmar."""
import json
import logging
import os
from datetime import date, datetime, timedelta

from flask import current_app

from app.extensions import db
from app.models import Loja, Produto, Receita

logger = logging.getLogger(__name__)


# Tool que o Claude pode usar pra criar pedido
CRIAR_PEDIDO_TOOL = {
    "name": "criar_pedido",
    "description": (
        "Cria um pedido da loja pra producao. Use quando o usuario pedir pra "
        "encomendar/pedir/solicitar produtos pra uma data especifica. "
        "NAO executa diretamente — apenas estrutura os dados pra preview."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "loja_id": {
                "type": ["integer", "null"],
                "description": "ID da loja destinataria. Null se nao foi mencionada e o sistema deve perguntar.",
            },
            "data_entrega": {
                "type": "string",
                "description": (
                    "Data de entrega no formato YYYY-MM-DD. "
                    "Resolva expressoes como 'amanha', 'sexta', 'proxima segunda'. "
                    "Se nao tiver data, use o proximo dia util."
                ),
            },
            "itens": {
                "type": "array",
                "description": "Lista de itens do pedido. Cada item tem nome (texto livre do usuario) e quantidade.",
                "items": {
                    "type": "object",
                    "properties": {
                        "nome": {
                            "type": "string",
                            "description": "Nome do produto/receita conforme o catalogo. Use o nome EXATO do catalogo.",
                        },
                        "quantidade": {"type": "integer", "minimum": 1},
                    },
                    "required": ["nome", "quantidade"],
                },
                "minItems": 1,
            },
            "observacao": {
                "type": ["string", "null"],
                "description": "Observacao opcional do pedido.",
            },
        },
        "required": ["data_entrega", "itens"],
    },
}


def _catalogo_texto():
    """Lista produtos + receitas formatados pra contexto do LLM."""
    linhas = []
    linhas.append("PRODUTOS DISPONIVEIS (use o nome exato):")
    for p in Produto.query.order_by(Produto.nome).all():
        linhas.append(f"  - {p.nome}")
    linhas.append("")
    linhas.append("RECEITAS DISPONIVEIS (use o nome exato):")
    for r in Receita.query.order_by(Receita.nome).all():
        linhas.append(f"  - {r.nome}")
    return "\n".join(linhas)


def _lojas_texto(user):
    """Lista lojas do usuario (admin ve todas)."""
    if user.is_admin():
        lojas = Loja.query.filter_by(ativa=True).order_by(Loja.nome).all()
    else:
        lojas = [user.loja] if user.loja_id and user.loja else []
    if not lojas:
        return "(nenhuma loja disponivel)"
    return "\n".join(f"  - id={l.id}: {l.nome}" for l in lojas)


def _build_system_prompt(user):
    hoje = date.today().isoformat()
    return f"""Voce e' um assistente de pedidos de uma padaria. Seu trabalho e' interpretar
comandos em linguagem natural do usuario e estruturar pedidos pra a producao.

Hoje e' {hoje}.

LOJAS DISPONIVEIS:
{_lojas_texto(user)}

{_catalogo_texto()}

REGRAS:
- Use a tool 'criar_pedido' quando o usuario quiser fazer um pedido.
- Para cada item mencionado, use o nome EXATO do catalogo. Se 'croissants' for
  ambiguo (varios tipos), use o tipo mais provavel ('Croissant Tradicional')
  e DEIXE CLARO na sua resposta-texto que o usuario deve confirmar.
- Datas relativas: resolva 'amanha', 'sexta', 'segunda', etc. pra YYYY-MM-DD.
- Se nao tiver loja mencionada e o usuario for admin com varias lojas,
  use loja_id=null e EXPLIQUE que o usuario precisa escolher.
- Se algo for ambiguo demais (produto inexistente, data invalida), NAO use
  a tool — apenas responda em texto pedindo clarificacao.
- Responda sempre em portugues brasileiro, lowercase, conciso.
"""


def interpretar(prompt_text, user):
    """Chama Claude pra interpretar o prompt. Retorna dict com:
    - tipo: 'criar_pedido' | 'conversa' | 'erro'
    - params: dict (se tipo == criar_pedido)
    - explicacao: str
    - raw: o JSON completo da resposta (pra audit)
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY') or current_app.config.get('ANTHROPIC_API_KEY')
    if not api_key:
        return {
            'tipo': 'erro',
            'explicacao': 'Copilot indisponivel: ANTHROPIC_API_KEY nao configurada no servidor.',
            'raw': None,
        }

    try:
        import anthropic
    except ImportError:
        return {
            'tipo': 'erro',
            'explicacao': 'Copilot indisponivel: biblioteca anthropic nao instalada.',
            'raw': None,
        }

    client = anthropic.Anthropic(api_key=api_key)
    system = _build_system_prompt(user)

    try:
        response = client.messages.create(
            model='claude-haiku-4-5',
            max_tokens=2000,
            system=[
                # cache_control no system pra reusar entre chamadas (catalogo nao muda toda hora)
                {'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}},
            ],
            tools=[CRIAR_PEDIDO_TOOL],
            messages=[{'role': 'user', 'content': prompt_text}],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception('Copilot: erro ao chamar Anthropic')
        return {
            'tipo': 'erro',
            'explicacao': f'Erro na chamada Anthropic: {exc}',
            'raw': None,
        }

    # Extrai tool_use e text
    tool_call = None
    texto_partes = []
    for block in response.content:
        if block.type == 'tool_use' and block.name == 'criar_pedido':
            tool_call = block.input
        elif block.type == 'text':
            texto_partes.append(block.text)

    explicacao = ' '.join(texto_partes).strip() or '(sem comentario do copilot)'
    raw = {
        'stop_reason': response.stop_reason,
        'usage': {
            'input': response.usage.input_tokens,
            'output': response.usage.output_tokens,
            'cache_read': getattr(response.usage, 'cache_read_input_tokens', 0),
            'cache_create': getattr(response.usage, 'cache_creation_input_tokens', 0),
        },
    }

    if tool_call:
        # Enriquece com matches de produto pra preview
        params = _enriquecer_params_pedido(tool_call, user)
        return {
            'tipo': 'criar_pedido',
            'params': params,
            'explicacao': explicacao,
            'raw': raw,
        }
    return {
        'tipo': 'conversa',
        'explicacao': explicacao,
        'raw': raw,
    }


def _enriquecer_params_pedido(tool_input, user):
    """Resolve nomes de produtos em IDs (produto_id ou receita_id).
    Retorna params com cada item enriquecido."""
    itens_enriquecidos = []
    for item in (tool_input.get('itens') or []):
        nome = (item.get('nome') or '').strip()
        qtd = int(item.get('quantidade') or 0)
        if not nome or qtd <= 0:
            continue
        match = _resolver_produto(nome)
        itens_enriquecidos.append({
            'nome_original': nome,
            'quantidade': qtd,
            'matches': match,
            # Pega o melhor match (1o exato, senao 1o fuzzy)
            'resolvido': match[0] if match else None,
        })

    # Resolve loja
    loja_id = tool_input.get('loja_id')
    loja_nome = None
    if loja_id:
        l = Loja.query.get(loja_id)
        if l:
            loja_nome = l.nome
        else:
            loja_id = None

    return {
        'loja_id': loja_id,
        'loja_nome': loja_nome,
        'data_entrega': tool_input.get('data_entrega'),
        'itens': itens_enriquecidos,
        'observacao': tool_input.get('observacao'),
    }


def _resolver_produto(nome):
    """Busca produto/receita por nome. Retorna lista de matches (exato primeiro)."""
    from sqlalchemy import func
    nome_norm = nome.strip()
    matches = []

    # Exato (case-insensitive)
    p = Produto.query.filter(func.lower(Produto.nome) == nome_norm.lower()).first()
    if p:
        matches.append({'tipo': 'produto', 'id': p.id, 'nome': p.nome, 'match': 'exato'})
    r = Receita.query.filter(func.lower(Receita.nome) == nome_norm.lower()).first()
    if r:
        matches.append({'tipo': 'receita', 'id': r.id, 'nome': r.nome, 'match': 'exato'})

    if matches:
        return matches

    # Fuzzy: contains
    for p in Produto.query.filter(Produto.nome.ilike(f'%{nome_norm}%')).limit(5).all():
        matches.append({'tipo': 'produto', 'id': p.id, 'nome': p.nome, 'match': 'fuzzy'})
    for r in Receita.query.filter(Receita.nome.ilike(f'%{nome_norm}%')).limit(5).all():
        matches.append({'tipo': 'receita', 'id': r.id, 'nome': r.nome, 'match': 'fuzzy'})

    return matches


def executar_criar_pedido(params, user):
    """Executa a criacao do pedido. params ja deve estar validado pela UI
    (usuario aprovou no preview, possivelmente editou os itens)."""
    from app.models import PedidoLoja, PedidoItem

    loja_id = params.get('loja_id')
    if not loja_id:
        return {'ok': False, 'erro': 'Loja nao especificada'}
    loja = Loja.query.get(loja_id)
    if not loja:
        return {'ok': False, 'erro': f'Loja {loja_id} nao encontrada'}

    data_str = params.get('data_entrega')
    try:
        data_entrega = datetime.strptime(data_str, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return {'ok': False, 'erro': f'Data invalida: {data_str}'}

    itens = params.get('itens') or []
    if not itens:
        return {'ok': False, 'erro': 'Pedido sem itens'}

    pedido = PedidoLoja(
        loja_id=loja_id,
        data_entrega=data_entrega,
        observacao=(params.get('observacao') or '').strip() or None,
        criado_por=user.id,
        status='pendente',
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
        pi = PedidoItem(
            pedido_id=pedido.id,
            quantidade=qtd,
        )
        if resolvido['tipo'] == 'produto':
            pi.produto_id = resolvido['id']
        elif resolvido['tipo'] == 'receita':
            pi.receita_id = resolvido['id']
        db.session.add(pi)
        salvos += 1

    if salvos == 0:
        db.session.rollback()
        return {
            'ok': False,
            'erro': f'Nenhum item pode ser resolvido. Nao encontrei: {", ".join(nao_resolvidos)}',
        }

    db.session.commit()
    return {
        'ok': True,
        'pedido_id': pedido.id,
        'itens_salvos': salvos,
        'nao_resolvidos': nao_resolvidos,
    }

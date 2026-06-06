"""Bot de atendimento ao cliente (WhatsApp via Agent Bot do Chatwoot).

FASE 2: além das boas-vindas/info, o bot usa ferramentas pra consultar
produtos (preço/estoque/SKU), montar link de carrinho e consultar pedido —
e passa pro humano quando o cliente pede, em reclamações, ou pra entrega/CEP
(que ainda não é automático). Reusa o Claude do copilot.

Ferramentas em `app.services.bot_tools`; prompt em `app.services.chatbot_prompt`.
"""
import json
import logging
import os

from flask import current_app

from app.services.chatbot_prompt import PROMPT

logger = logging.getLogger(__name__)

MODELO = 'claude-sonnet-4-6'
MAX_ITERACOES = 6  # teto de idas-e-voltas de ferramenta por mensagem
_FALLBACK = 'Já te passo para um atendente pra te ajudar melhor. 🙂'

TOOL_HANDOFF = {
    'name': 'transferir_para_humano',
    'description': (
        'Passa a conversa para um atendente humano. Use quando o cliente pedir '
        'para falar com uma pessoa, em reclamações, em dúvidas de entrega/CEP/'
        'agendamento, cartinha em pedido já feito, ou quando não tiver certeza.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'mensagem_cliente': {
                'type': 'string',
                'description': 'Mensagem curta e gentil avisando que um atendente vai continuar.',
            },
            'motivo': {'type': 'string', 'description': 'Motivo curto (uso interno).'},
        },
        'required': ['mensagem_cliente'],
    },
}

TOOLS = [
    {
        'name': 'consultar_produtos',
        'description': 'Busca produtos no catálogo do site (nome, preço, '
                       'disponibilidade e SKU). Use SEMPRE antes de sugerir um '
                       'produto ou montar um link.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'busca': {'type': 'string',
                          'description': 'termo de busca, ex: "croissant amêndoas"'},
            },
            'required': ['busca'],
        },
    },
    {
        'name': 'consultar_pedido',
        'description': 'Consulta o status de um pedido pelo número informado pelo cliente.',
        'input_schema': {
            'type': 'object',
            'properties': {'numero': {'type': 'string'}},
            'required': ['numero'],
        },
    },
    {
        'name': 'gerar_link_carrinho',
        'description': 'Monta o link do carrinho a partir dos itens (SKU + '
                       'quantidade). Use os SKUs vindos do consultar_produtos. '
                       'Nunca inclua SKU de cesta aqui.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'itens': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'sku': {'type': 'string'},
                            'qtd': {'type': 'integer'},
                        },
                        'required': ['sku', 'qtd'],
                    },
                },
            },
            'required': ['itens'],
        },
    },
    TOOL_HANDOFF,
]


def disponivel():
    return bool(os.environ.get('ANTHROPIC_API_KEY')
                or current_app.config.get('ANTHROPIC_API_KEY'))


def _executar_tool(nome, inp):
    from app.services import bot_tools
    try:
        if nome == 'consultar_produtos':
            return bot_tools.consultar_produtos(inp.get('busca') or inp.get('query') or '')
        if nome == 'consultar_pedido':
            return bot_tools.consultar_pedido(inp.get('numero') or inp.get('numero_pedido') or '')
        if nome == 'gerar_link_carrinho':
            return bot_tools.gerar_link_carrinho(inp.get('itens') or [])
        return {'erro': f'ferramenta desconhecida: {nome}'}
    except Exception as exc:  # noqa: BLE001
        logger.exception('bot tool %s falhou', nome)
        return {'erro': str(exc)}


def _build_messages(historico):
    messages = []
    for m in (historico or []):
        role = m.get('role')
        content = (m.get('content') or '').strip()
        if role in ('user', 'assistant') and content:
            messages.append({'role': role, 'content': content})
    while messages and messages[0]['role'] != 'user':
        messages = messages[1:]
    if len(messages) > 20:
        messages = messages[-20:]
        while messages and messages[0]['role'] != 'user':
            messages = messages[1:]
    return messages


def responder(historico):
    """Processa a conversa (com loop de ferramentas) e decide a resposta.

    `historico`: lista cronológica [{'role','content'}] terminando na última
    mensagem do cliente.

    Retorna:
      {'acao': 'responder', 'texto': str}
      {'acao': 'handoff',   'texto': str, 'motivo': str}
    """
    api_key = (os.environ.get('ANTHROPIC_API_KEY')
               or current_app.config.get('ANTHROPIC_API_KEY'))
    if not api_key:
        return {'acao': 'handoff', 'texto': _FALLBACK, 'motivo': 'sem ANTHROPIC_API_KEY'}
    try:
        import anthropic
    except ImportError:
        return {'acao': 'handoff', 'texto': _FALLBACK, 'motivo': 'lib anthropic ausente'}

    messages = _build_messages(historico)
    if not messages:
        return {'acao': 'handoff', 'texto': _FALLBACK, 'motivo': 'sem mensagem'}

    # Cache breakpoint na ultima tool (schema) + no system: ~corta custo.
    tools_cache = [dict(t) for t in TOOLS]
    tools_cache[-1] = {**tools_cache[-1], 'cache_control': {'type': 'ephemeral'}}

    try:
        client = anthropic.Anthropic(api_key=api_key)
    except Exception as exc:  # noqa: BLE001
        logger.exception('chatbot: erro criando client')
        return {'acao': 'handoff', 'texto': _FALLBACK, 'motivo': f'client: {exc}'}

    for _ in range(MAX_ITERACOES):
        try:
            resp = client.messages.create(
                model=MODELO,
                max_tokens=1200,
                system=[{'type': 'text', 'text': PROMPT,
                         'cache_control': {'type': 'ephemeral'}}],
                tools=tools_cache,
                messages=messages,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception('chatbot: erro Anthropic')
            return {'acao': 'handoff', 'texto': _FALLBACK, 'motivo': f'erro anthropic: {exc}'}

        tool_uses = [b for b in resp.content if getattr(b, 'type', None) == 'tool_use']

        if not tool_uses:
            texto = '\n'.join(b.text for b in resp.content
                              if getattr(b, 'type', None) == 'text' and b.text).strip()
            if not texto:
                return {'acao': 'handoff', 'texto': 'Já te passo para um atendente. 🙂',
                        'motivo': 'resposta vazia'}
            return {'acao': 'responder', 'texto': texto}

        # Handoff tem prioridade — encerra o loop na hora.
        for b in tool_uses:
            if b.name == 'transferir_para_humano':
                inp = b.input or {}
                return {
                    'acao': 'handoff',
                    'texto': (inp.get('mensagem_cliente') or '').strip()
                             or 'Já te passo para um atendente. 🙂',
                    'motivo': inp.get('motivo') or 'handoff',
                }

        # Executa as ferramentas e devolve os resultados pro Claude.
        messages.append({'role': 'assistant', 'content': resp.content})
        resultados = []
        for b in tool_uses:
            out = _executar_tool(b.name, b.input or {})
            resultados.append({
                'type': 'tool_result',
                'tool_use_id': b.id,
                'content': json.dumps(out, ensure_ascii=False),
            })
        messages.append({'role': 'user', 'content': resultados})

    # Estourou o teto de iteracoes — passa pro humano por seguranca.
    return {'acao': 'handoff', 'texto': _FALLBACK, 'motivo': 'limite de passos'}

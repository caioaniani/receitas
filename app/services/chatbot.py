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

# Janela de atendimento humano (BRT). O bot CONTINUA respondendo fora dela
# (consulta produtos, manda link, etc) — mas quando vai FAZER HANDOFF fora
# da janela, avisa que ninguem vai pegar agora e a equipe responde de manha.
# Decisao do dono 14/06/2026.
HORARIO_CHAT_INICIO = 6   # 06:00
HORARIO_CHAT_FIM = 20     # 20:00 (exclusivo: 19:59 ainda dentro)


def _fora_horario_chat():
    from app.utils import agora
    h = agora().hour
    return h < HORARIO_CHAT_INICIO or h >= HORARIO_CHAT_FIM


def _texto_handoff_com_horario(texto):
    """Se estiver fora da janela de atendimento (06-20), prepend um aviso
    explicito ao texto que o bot vai mandar pro cliente no handoff. Sem
    isso, o cliente fica esperando atendente as 23h sem saber que ninguem
    vai pegar agora.

    Idempotente: se o LLM ja escreveu o aviso (mensagem ja contem '06:00'),
    nao duplica."""
    if not _fora_horario_chat():
        return texto
    base = (texto or '').strip()
    if '06:00' in base:
        return base
    aviso = ('Estamos fora do nosso horário de atendimento aqui no chat '
             '(06:00 às 20:00). Vou registrar sua mensagem e nossa equipe '
             'te responde a partir das 06:00 da manhã. ')
    return aviso + base


def _resp_handoff(texto, motivo, tools_usadas=None):
    """Constroi o dict de handoff aplicando o aviso de fora-horario no texto.
    Centraliza pra TODOS os caminhos de handoff (fallback de erro, tool,
    teto de iteracoes) usarem a mesma garantia — sem isso, fallbacks que
    nao passam pelo LLM ignoram a regra de horario."""
    out = {
        'acao': 'handoff',
        'texto': _texto_handoff_com_horario(texto),
        'motivo': motivo,
    }
    if tools_usadas is not None:
        out['tools_usadas'] = tools_usadas
    return out

# Quantas mensagens guardar no nosso store por conversa (cap). O Claude ja
# recebe so as ultimas 20 (`_build_messages`), entao 40 cobre folgado.
MAX_HIST_STORE = 40


def carregar_historico(conv_id):
    """Le o historico persistido desta conversa do NOSSO banco
    (`ChatbotConversa`). Retorna lista [{role, content}] ou [] se nao houver.

    Fonte confiavel de contexto — nao depende da API do Chatwoot, que falha
    intermitentemente e fazia o bot 'esquecer' a conversa."""
    from app.models import ChatbotConversa
    conv = ChatbotConversa.query.filter_by(conv_id=str(conv_id)).first()
    if not conv:
        return []
    try:
        h = json.loads(conv.mensagens_json or '[]')
        return h if isinstance(h, list) else []
    except (ValueError, TypeError):
        return []


def salvar_historico(conv_id, historico, resposta):
    """Persiste o turno no nosso banco: o historico efetivo (que JA inclui a
    msg atual do cliente) + a resposta do bot. So texto — imagens nao vao pro
    store (o `_build_messages` so usa imagem da ULTIMA msg, que sempre vem
    fresca do webhook). Capa nas ultimas MAX_HIST_STORE."""
    from app.extensions import db
    from app.models import ChatbotConversa
    msgs = []
    for m in (historico or []):
        role = m.get('role')
        if role not in ('user', 'assistant'):
            continue
        c = (m.get('content') or '').strip()
        if not c and m.get('imagens'):
            c = '[imagem enviada]'
        if c:
            msgs.append({'role': role, 'content': c})
    if resposta and resposta.strip():
        msgs.append({'role': 'assistant', 'content': resposta.strip()})
    msgs = msgs[-MAX_HIST_STORE:]
    try:
        conv = ChatbotConversa.query.filter_by(conv_id=str(conv_id)).first()
        if not conv:
            conv = ChatbotConversa(conv_id=str(conv_id))
            db.session.add(conv)
        conv.mensagens_json = json.dumps(msgs, ensure_ascii=False)
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        logger.exception('chatbot salvar_historico falhou conv=%s', conv_id)
# Mensagem segura quando a consulta de catalogo falha: NUNCA responder preco
# de memoria (risco de inventar valor — dinheiro). Passa pro humano.
_FALLBACK_CATALOGO = ('Tive uma instabilidade pra consultar nosso catálogo '
                      'agora. Já te passo para um atendente continuar com você, '
                      'tá? 🙂')

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
                       'disponibilidade, SKU e DESCRIÇÃO/conteúdo da cesta). Use '
                       'SEMPRE antes de sugerir um produto, montar um link, ou '
                       'responder o que vem numa cesta.',
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
        'name': 'consultar_ingredientes',
        'description': ('Consulta a receita REAL do produto e devolve a lista '
                        'de ingredientes (com percentual). Use SOMENTE em '
                        'pergunta informativa ("tem leite no croissant?", '
                        '"esse pão tem ovo?"). NÃO use se o cliente disser que '
                        'TEM alergia/intolerância — alergia = handoff direto '
                        '(ver seção ALERGIA do prompt).'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'nome_produto': {
                    'type': 'string',
                    'description': 'nome do produto, ex: "Sourdough Tradicional"'},
            },
            'required': ['nome_produto'],
        },
    },
    {
        'name': 'consultar_pedido',
        'description': ('Consulta status, itens e a DATA DE ENTREGA real (a '
                        'agendada) de um pedido pelo número. Use pra dar a data '
                        'correta quando o cliente tiver dúvida — o site às vezes '
                        'mostra "entregue hoje" por bug; esta data é a verdadeira.\n\n'
                        'AUTORIZAÇÃO: a tool valida que o solicitante é o dono '
                        'do pedido (por telefone do canal OU CPF). Se vier '
                        '`erro: autorizacao_necessaria`, peça o CPF ao cliente '
                        'e chame de novo com `cpf_cliente`.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'numero': {'type': 'string',
                            'description': 'numero do pedido'},
                'cpf_cliente': {
                    'type': 'string',
                    'description': ('CPF do comprador do pedido — só preencha '
                                     'se o cliente JÁ informou na conversa.'),
                },
            },
            'required': ['numero'],
        },
    },
    {
        'name': 'editar_cartinha_pedido',
        'description': ('Cria ou ALTERA a cartinha/mensagem de um pedido JÁ '
                        'FEITO no site. A cartinha aparece pra equipe na tela '
                        'de impressão de entregas — a versão manual sobrescreve '
                        'a do site. Use quando o cliente disser "adiciona uma '
                        'cartinha", "muda a mensagem", "esqueci de pôr a '
                        'cartinha". PEÇA O NÚMERO DO PEDIDO + o TEXTO da '
                        'cartinha (palavras exatas) antes de chamar. NÃO use '
                        'pra produto/pedido novo — nesse caso o cliente '
                        'preenche no checkout do site.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'numero_pedido': {
                    'type': 'string',
                    'description': 'numero do pedido do site (ex: "12345")'},
                'texto_cartinha': {
                    'type': 'string',
                    'description': 'texto exato da cartinha que o cliente quer'},
            },
            'required': ['numero_pedido', 'texto_cartinha'],
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
    {
        'name': 'consultar_frete',
        'description': ('Estima o frete de entrega pra um CEP ou endereço do '
                        'cliente (anéis de distância a partir da padaria: '
                        'grátis até 1 km, +R$5 por km, máximo 15 km). Prefira '
                        'o CEP quando o cliente tiver. O valor é estimativa — '
                        'o definitivo é o do checkout do site.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'endereco_ou_cep': {
                    'type': 'string',
                    'description': 'CEP (melhor) ou endereço com bairro/cidade'},
            },
            'required': ['endereco_ou_cep'],
        },
    },
    {
        'name': 'buscar_nota_fiscal',
        'description': ('Busca a Nota Fiscal de um pedido pelo CPF do comprador '
                        'E o número do pedido (precisa dos DOIS pra evitar vazar '
                        'NF de outro cliente). Use SO pra pedidos do SITE. Para '
                        'pedidos B2B/locais, use transferir_para_humano.'),
        'input_schema': {
            'type': 'object',
            'properties': {
                'cpf': {'type': 'string',
                        'description': 'CPF do comprador (so digitos ou formatado)'},
                'numero_pedido': {'type': 'string',
                                   'description': 'numero do pedido informado pelo cliente'},
            },
            'required': ['cpf', 'numero_pedido'],
        },
    },
    TOOL_HANDOFF,
]


def disponivel():
    return bool(os.environ.get('ANTHROPIC_API_KEY')
                or current_app.config.get('ANTHROPIC_API_KEY'))


def _executar_tool(nome, inp, *, telefone_contato=None):
    """Executa a tool. `telefone_contato` (canonico, vindo do canal — ex:
    Chatwoot WhatsApp) eh injetado em tools que precisam autorizar dono
    de pedido. Nunca vem do LLM, sempre do contexto da conversa."""
    from app.services import bot_tools
    try:
        if nome == 'consultar_produtos':
            return bot_tools.consultar_produtos(inp.get('busca') or inp.get('query') or '')
        if nome == 'consultar_ingredientes':
            return bot_tools.consultar_ingredientes(
                inp.get('nome_produto') or inp.get('nome') or inp.get('produto') or '')
        if nome == 'consultar_pedido':
            return bot_tools.consultar_pedido(
                inp.get('numero') or inp.get('numero_pedido') or '',
                telefone_contato=telefone_contato,
                cpf_cliente=inp.get('cpf_cliente') or inp.get('cpf') or None)
        if nome == 'editar_cartinha_pedido':
            return bot_tools.editar_cartinha_pedido(
                inp.get('numero_pedido') or inp.get('numero') or '',
                inp.get('texto_cartinha') or inp.get('texto') or inp.get('cartinha') or '',
                telefone_contato=telefone_contato,
                cpf_cliente=inp.get('cpf_cliente') or inp.get('cpf') or None)
        if nome == 'gerar_link_carrinho':
            return bot_tools.gerar_link_carrinho(inp.get('itens') or [])
        if nome == 'consultar_frete':
            from app.services import frete
            return frete.consultar_frete(
                inp.get('endereco_ou_cep') or inp.get('cep')
                or inp.get('endereco') or '')
        if nome == 'buscar_nota_fiscal':
            return bot_tools.buscar_nota_fiscal(
                inp.get('cpf') or '',
                inp.get('numero_pedido') or inp.get('numero') or '')
        return {'erro': f'ferramenta desconhecida: {nome}'}
    except Exception as exc:  # noqa: BLE001
        logger.exception('bot tool %s falhou', nome)
        return {'erro': str(exc)}


def _build_messages(historico):
    """Converte o historico [{'role','content','imagens'?}] em mensagens da API.

    Imagens (anexos do Chatwoot) entram como blocos de imagem SO na ultima
    mensagem do cliente — a atual — pra o Claude enxergar o que a pessoa mandou
    sem inflar custo com fotos antigas da conversa."""
    raw = []
    for m in (historico or []):
        role = m.get('role')
        if role not in ('user', 'assistant'):
            continue
        content = (m.get('content') or '').strip()
        imagens = m.get('imagens') or []
        if not content and not imagens:
            continue
        raw.append({'role': role, 'content': content, 'imagens': imagens})

    while raw and raw[0]['role'] != 'user':
        raw = raw[1:]
    if len(raw) > 20:
        raw = raw[-20:]
        while raw and raw[0]['role'] != 'user':
            raw = raw[1:]

    idx_ultimo_user = max((i for i, m in enumerate(raw) if m['role'] == 'user'),
                          default=-1)
    messages = []
    for i, m in enumerate(raw):
        if m['imagens'] and i == idx_ultimo_user:
            from app.services import chatwoot
            blocos = []
            for url in m['imagens'][:4]:
                img = chatwoot.baixar_imagem(url)
                if img:
                    media_type, b64 = img
                    blocos.append({'type': 'image', 'source': {
                        'type': 'base64', 'media_type': media_type, 'data': b64}})
            blocos.append({'type': 'text',
                           'text': m['content'] or 'O cliente enviou esta imagem.'})
            messages.append({'role': 'user', 'content': blocos})
        else:
            messages.append({'role': m['role'], 'content': m['content']})
    return messages


def responder(historico):
    """Processa a conversa (com loop de ferramentas) e decide a resposta.

    `historico`: lista cronológica [{'role','content'}] terminando na última
    mensagem do cliente.

    Retorna:
      {'acao': 'responder', 'texto': str}
      {'acao': 'handoff',   'texto': str, 'motivo': str}
    """
    # Fora-horario NAO bloqueia mais o bot (decisao do dono 14/06/2026).
    # O bot continua respondendo normal; quem injeta o aviso de horario eh o
    # branch de handoff abaixo (via `_texto_handoff_com_horario`).
    api_key = (os.environ.get('ANTHROPIC_API_KEY')
               or current_app.config.get('ANTHROPIC_API_KEY'))
    if not api_key:
        return _resp_handoff(_FALLBACK, 'sem ANTHROPIC_API_KEY')
    try:
        import anthropic
    except ImportError:
        return _resp_handoff(_FALLBACK, 'lib anthropic ausente')

    messages = _build_messages(historico)
    if not messages:
        return _resp_handoff(_FALLBACK, 'sem mensagem')

    # Cache breakpoint na ultima tool (schema) + no system: ~corta custo.
    tools_cache = [dict(t) for t in TOOLS]
    tools_cache[-1] = {**tools_cache[-1], 'cache_control': {'type': 'ephemeral'}}

    try:
        client = anthropic.Anthropic(api_key=api_key)
    except Exception as exc:  # noqa: BLE001
        logger.exception('chatbot: erro criando client')
        return _resp_handoff(_FALLBACK, f'client: {exc}')

    # Rastreia se a ULTIMA consulta de catalogo desta rodada falhou. Se falhou,
    # o bot NAO pode responder preco/produto (poderia inventar) — forca handoff.
    produto_falhou = False
    # Quais ferramentas o bot usou neste turno. Vai no resultado pra o
    # VIGIA saber se um handoff foi 'preguicoso' (transferiu sem nem
    # consultar o catalogo — caso real 12/06/2026, conv #198: cliente
    # perguntou de cesta+entrega e o bot fez handoff com zero consulta).
    tools_usadas = []

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
            return _resp_handoff(_FALLBACK, f'erro anthropic: {exc}',
                                 tools_usadas=tools_usadas)

        tool_uses = [b for b in resp.content if getattr(b, 'type', None) == 'tool_use']

        if not tool_uses:
            # Salvaguarda de dinheiro: se a consulta de catalogo falhou nesta
            # rodada, nao confiamos no texto (pode conter preco inventado).
            if produto_falhou:
                logger.warning('crm bot: consultar_produtos falhou -> handoff '
                               '(evita preco inventado)')
                return _resp_handoff(_FALLBACK_CATALOGO,
                                     'consultar_produtos falhou',
                                     tools_usadas=tools_usadas)
            texto = '\n'.join(b.text for b in resp.content
                              if getattr(b, 'type', None) == 'text' and b.text).strip()
            if not texto:
                return _resp_handoff('Já te passo para um atendente. 🙂',
                                     'resposta vazia',
                                     tools_usadas=tools_usadas)
            return {'acao': 'responder', 'texto': texto,
                    'tools_usadas': tools_usadas}

        # Handoff tem prioridade — encerra o loop na hora.
        for b in tool_uses:
            if b.name == 'transferir_para_humano':
                inp = b.input or {}
                texto_base = ((inp.get('mensagem_cliente') or '').strip()
                              or 'Já te passo para um atendente. 🙂')
                return _resp_handoff(texto_base,
                                     inp.get('motivo') or 'handoff',
                                     tools_usadas=tools_usadas)

        # Executa as ferramentas e devolve os resultados pro Claude.
        messages.append({'role': 'assistant', 'content': resp.content})
        resultados = []
        for b in tool_uses:
            out = _executar_tool(b.name, b.input or {})
            tools_usadas.append(b.name)
            if b.name == 'consultar_produtos':
                produto_falhou = bool(isinstance(out, dict) and out.get('erro'))
            resultados.append({
                'type': 'tool_result',
                'tool_use_id': b.id,
                'content': json.dumps(out, ensure_ascii=False),
            })
        messages.append({'role': 'user', 'content': resultados})

    # Estourou o teto de iteracoes — passa pro humano por seguranca.
    return _resp_handoff(_FALLBACK, 'limite de passos',
                         tools_usadas=tools_usadas)


# ── Follow-up automatico (bot retoma cliente que sumiu) ────────────────────
#
# Pedido do dono (12/06/2026, conversa #186 — Bethania): quando o CLIENTE
# para de responder depois de uma mensagem NOSSA, o bot manda um cutucao
# gentil ("Conseguiu finalizar?") em vez de so alertar o dono. O cutucao
# humano equivalente reativou a venda na hora.
#
# Guarda-corpos (codigo, nao confianca no modelo):
# - So conversa `pending` (turno do bot — listar_conversas_paradas ja filtra).
# - So quando a ULTIMA mensagem e NOSSA (cliente silencioso). Se a ultima e
#   do cliente, o problema e outro (bot nao respondeu) — nao cutucar.
# - Janela: CHATBOT_FOLLOWUP_MIN (default 5) ate CHATBOT_FOLLOWUP_MAX_MIN
#   (default 120). Conversa fria nao recebe cutucao 19h depois.
# - UMA vez por conversa, persistido em VigiaVeredito ('[FOLLOWUP') —
#   sobrevive a deploy, mesmo padrao do dedupe de abandono.
# - Teto por ciclo (CHATBOT_FOLLOWUP_MAX_POR_CICLO, default 3).
# - Kill-switch: CHATBOT_FOLLOWUP=0.

FOLLOWUP_MODELO = 'claude-haiku-4-5-20251001'

FOLLOWUP_PROMPT = (
    'Você é o atendente virtual da padaria O Pão. A conversa abaixo parou: '
    'o cliente não responde há {minutos} minutos e a última mensagem é '
    'nossa. Escreva UMA mensagem curta (1 a 2 frases, PT-BR, tom leve e '
    'gentil, no máximo 1 emoji) pra retomar — ex: perguntar se conseguiu '
    'finalizar o pedido ou se ficou alguma dúvida. NÃO invente preço, '
    'prazo, promoção nem informação nova. NÃO repita links. Responda '
    'APENAS com o texto da mensagem, sem aspas.'
)


def _followup_ja_enviado(conv_id, horas=48):
    from datetime import timedelta

    from app.models import VigiaVeredito
    from app.utils import agora
    try:
        corte = agora() - timedelta(hours=horas)
        return (VigiaVeredito.query
                .filter(VigiaVeredito.conv_id == str(conv_id),
                        VigiaVeredito.criado_em >= corte,
                        VigiaVeredito.mensagem_cliente.like('[FOLLOWUP%'))
                .first()) is not None
    except Exception:  # noqa: BLE001
        logger.exception('followup: dedupe falhou (assume ja enviado)')
        return True   # fail-closed: na duvida, NAO manda de novo


def _followup_registrar(conv_id, nome_contato, minutos, texto, enviado):
    from app.extensions import db
    from app.models import VigiaVeredito
    try:
        db.session.add(VigiaVeredito(
            conv_id=str(conv_id),
            cliente=(nome_contato or '')[:200] or None,
            mensagem_cliente=f'[FOLLOWUP {minutos}min]',
            bot_acao='followup',
            motivo_vigia=(texto or '')[:1000] or None,
            alerta=False,
            enviado_whatsapp=bool(enviado),
        ))
        db.session.commit()
    except Exception:  # noqa: BLE001
        logger.exception('followup: registro falhou')
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


def _followup_gerar_texto(api_key, historico, minutos):
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    linhas = []
    for m in (historico or [])[-10:]:
        content = (m.get('content') or '').strip()
        if not content:
            continue
        quem = 'CLIENTE' if m.get('role') == 'user' else 'NOS'
        linhas.append(f'{quem}: {content}')
    resp = client.messages.create(
        model=FOLLOWUP_MODELO,
        max_tokens=150,
        system=FOLLOWUP_PROMPT.format(minutos=minutos),
        messages=[{'role': 'user', 'content': '\n'.join(linhas) or '(vazio)'}],
    )
    texto = ''.join(b.text for b in resp.content
                    if getattr(b, 'type', None) == 'text' and b.text).strip()
    return texto.strip('"“” ').strip()


def followup_conversas_paradas():
    """Ciclo do follow-up: acha conversas pending com cliente silencioso
    na janela configurada e manda UMA mensagem de retomada por conversa.
    Retorna resumo {'avaliadas': n, 'enviadas': n} pro log/teste."""
    from app.services import chatwoot

    cfg = current_app.config
    if str(cfg.get('CHATBOT_FOLLOWUP', '1')) == '0':
        return {'pulou': 'desligado'}
    api_key = (os.environ.get('ANTHROPIC_API_KEY')
               or cfg.get('ANTHROPIC_API_KEY'))
    if not api_key:
        return {'pulou': 'sem ANTHROPIC_API_KEY'}

    min_sil = int(cfg.get('CHATBOT_FOLLOWUP_MIN', 5) or 5)
    max_sil = int(cfg.get('CHATBOT_FOLLOWUP_MAX_MIN', 120) or 120)
    max_ciclo = int(cfg.get('CHATBOT_FOLLOWUP_MAX_POR_CICLO', 3) or 3)

    paradas = chatwoot.listar_conversas_paradas(min_minutos=min_sil)
    avaliadas = enviadas = 0
    for c in paradas:
        if enviadas >= max_ciclo:
            break
        conv_id = c.get('id')
        minutos = c.get('minutos_paradas', 0)
        if not conv_id or minutos > max_sil:
            continue
        if _followup_ja_enviado(conv_id):
            continue
        historico = chatwoot.buscar_historico(conv_id)
        if not historico:
            continue
        # Ultima mensagem tem que ser NOSSA (cliente silencioso).
        ultima = historico[-1]
        if ultima.get('role') != 'assistant':
            continue
        avaliadas += 1
        try:
            texto = _followup_gerar_texto(api_key, historico, minutos)
        except Exception:  # noqa: BLE001
            logger.exception('followup: geracao falhou conv=%s', conv_id)
            continue
        if not texto:
            continue
        envio = chatwoot.enviar_mensagem(conv_id, texto)
        ok = bool(envio.get('ok'))
        _followup_registrar(conv_id, c.get('nome_contato') or '',
                            minutos, texto, ok)
        if ok:
            enviadas += 1
            logger.info('followup enviado conv=%s (%smin)', conv_id, minutos)
        else:
            logger.warning('followup falhou conv=%s: %s', conv_id, envio)
    return {'avaliadas': avaliadas, 'enviadas': enviadas}

"""Venda pelo WhatsApp: o bot atende quem quer COMPRAR e entrega o link do
carrinho (decisão do dono, 07/10/2026).

Substitui, só para compras, a regra de 24/09/2026 ("o robô não monta
carrinho nem manda o cliente para o site"). O resto do atendimento restrito
continua igual: reclamação, falha de entrega, pedido existente, alergia,
anexo, pedido de atendente e conversa já encaminhada ficam com a equipe.

Desenho (deliberadamente MENOR que o motor antigo de `chatbot.py`):
- o modelo só tem quatro ferramentas: catálogo, frete, montar o link do
  carrinho e passar para a equipe. Não consulta pedido, nota nem cadastro;
- o bot NUNCA cria pedido nem cobra: o cliente confere o carrinho, escolhe
  entrega/data e paga no site (`carrinho_link`, revalidado no servidor);
- travas determinísticas ANTES do modelo (pedido de atendente, falha
  operacional, reclamação, tentativa de manipulação) mandam direto para a
  equipe; qualquer falha da IA também;
- todo link que sair na resposta tem de ter vindo de uma ferramenta nesta
  conversa — link inventado vira passagem para a equipe.
Kill-switch: `BOT_VENDA=0` (volta ao atendimento restrito puro).
"""
import json
import logging
import os
import re

from flask import current_app

from app.services import ia_modelos

logger = logging.getLogger(__name__)

MAX_ITERACOES = 8
MAX_TOKENS = 4000
MAX_TOKENS_RETRY = 8000

PROMPT = """Você é a atendente virtual da O Pão Padaria Artesanal no WhatsApp. \
Seu único trabalho aqui é ajudar quem quer COMPRAR pelo site: entender o que \
a pessoa quer, mostrar as opções com preço e montar o carrinho, mandando o \
link para ela conferir, escolher entrega ou retirada e PAGAR no site.

Como atender:
- Escreva em português correto, curto e cordial. Sem gírias, sem "vc", "tá", \
"pra". Emojis só se o cliente usar.
- Use SEMPRE o consultar_produtos antes de falar de produto, preço ou \
disponibilidade. Nunca invente produto, preço, peso, composição ou data.
- Pergunte só o necessário (qual item, quantas unidades). Se o pedido estiver \
claro, monte o carrinho direto.
- Datas: diga o que o consultar_produtos informar (indisponivel_em, \
sob_encomenda = pedir com 2 dias de antecedência). Não confirme data, horário \
nem entrega: quem confirma é o site, na hora de finalizar.
- Frete: use o consultar_frete se o cliente perguntar; o valor é estimado e o \
definitivo aparece no site.
- Sourdough com "fatiavel": pergunte se quer fatiado e passe fatiado=true no \
item. Os outros não têm essa opção.
- Menu montável (campo "menu"): o cliente escolhe quantos de cada mini, \
somando EXATAMENTE o total do menu (respeite o máximo por item, quando \
houver). Mostre os minis e os preços; se o cliente não quiser escolher, use \
a composição padrão. Passe a composição com o pi_id de cada mini.
- Cartinha de presente: se o cliente quiser, escreva o texto com ele (máximo \
de {cartinha_max} caracteres), confirme o texto exato e passe em "cartinha". Não invente \
texto que ele não aprovou.
- Antes de montar o link, confirme o resumo (itens, quantidades, fatiado, \
minis escolhidos e cartinha). Depois use o montar_link_carrinho e mande o \
link EXATAMENTE como a ferramenta devolveu, com o resumo e o valor dos itens \
(sem frete). Explique que no link ele confere tudo, escolhe entrega ou \
retirada, data e horário e paga por Pix ou cartão.
- Se a ferramenta recusar (item esgotado, total do menu errado, cartinha \
longa), explique ao cliente e ajuste com ele.
- Pedido grande para evento, preço especial, encomenda fora do site, dúvida \
sobre pedido já feito, reclamação, alergia ou restrição alimentar, pagamento \
fora do site, troca, cancelamento, qualquer coisa que não seja montar uma \
compra do site: use transferir_para_humano com um motivo claro. Não tente \
resolver.
- Nunca diga que o pedido está feito ou pago: ele só existe depois que o \
cliente paga no site.
- Nunca cole link que não veio de uma ferramenta.
"""

TOOLS = [
    {
        'name': 'consultar_produtos',
        'description': 'Busca no catálogo do site. Devolve nome, kind, id, '
                       'preço, disponibilidade, datas indisponíveis, '
                       'sob_encomenda, fatiavel e, nos menus montáveis, o '
                       'bloco "menu" com total, máximo por item e os minis '
                       '(pi_id, nome, preço, quantidade padrão). Sem termo '
                       'conhecido devolve o catálogo resumido.',
        'input_schema': {
            'type': 'object',
            'properties': {'busca': {'type': 'string'}},
            'required': ['busca'],
        },
    },
    {
        'name': 'consultar_frete',
        'description': 'Estimativa de frete para um CEP ou endereço (o valor '
                       'definitivo é o do site).',
        'input_schema': {
            'type': 'object',
            'properties': {'endereco_ou_cep': {'type': 'string'}},
            'required': ['endereco_ou_cep'],
        },
    },
    {
        'name': 'montar_link_carrinho',
        'description': 'Monta o carrinho no site e devolve o link para o '
                       'cliente conferir e pagar. Use kind+id do '
                       'consultar_produtos. Menu montável exige composicao '
                       '(pi_id + quantidade) somando o total. fatiado só em '
                       'item fatiavel. cartinha opcional (respeite o '
                       'máximo de caracteres informado), só com texto '
                       'aprovado pelo cliente.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'itens': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'kind': {'type': 'string',
                                     'enum': ['receita', 'produto']},
                            'id': {'type': 'integer'},
                            'quantidade': {'type': 'integer'},
                            'fatiado': {'type': 'boolean'},
                            'composicao': {
                                'type': 'array',
                                'items': {
                                    'type': 'object',
                                    'properties': {
                                        'pi_id': {'type': 'integer'},
                                        'quantidade': {'type': 'integer'},
                                    },
                                    'required': ['pi_id', 'quantidade'],
                                },
                            },
                        },
                        'required': ['kind', 'id', 'quantidade'],
                    },
                },
                'cartinha': {'type': 'string'},
            },
            'required': ['itens'],
        },
    },
    {
        'name': 'transferir_para_humano',
        'description': 'Passa a conversa para a equipe. Use para tudo que '
                       'não for montar uma compra do site.',
        'input_schema': {
            'type': 'object',
            'properties': {'motivo': {'type': 'string'}},
            'required': ['motivo'],
        },
    },
]


def disponivel():
    """Venda ligada (kill-switch `BOT_VENDA`, padrão ligado) e chave da API
    presente. Sem isso o atendimento restrito segue encaminhando compras."""
    ligado = os.environ.get('BOT_VENDA',
                            current_app.config.get('BOT_VENDA', '1'))
    if str(ligado).strip() == '0':
        return False
    return bool(os.environ.get('ANTHROPIC_API_KEY')
                or current_app.config.get('ANTHROPIC_API_KEY'))


# ── Ferramentas ─────────────────────────────────────────────────────────

_MAX_ENRIQUECIDOS = 40


def consultar_produtos(busca):
    """`bot_tools.consultar_produtos` + o que a venda precisa saber de cada
    item: `fatiavel`, `sob_encomenda` e, no menu montável, os minis com o
    `pi_id` (endereço que o `montar_link_carrinho` exige)."""
    from app.services import bot_tools, loja_catalogo
    res = bot_tools.consultar_produtos(busca)
    for i, d in enumerate(res.get('produtos') or []):
        if i >= _MAX_ENRIQUECIDOS:
            break
        item = loja_catalogo.por_id_publicado(d.get('kind'), d.get('id'))
        if not item:
            continue
        if item.get('fatiavel'):
            d['fatiavel'] = True
        if item.get('sob_encomenda'):
            d['sob_encomenda'] = True
        menu = item.get('menu')
        if menu:
            d['menu'] = {
                'total': menu['total'],
                'max_por_item': menu['max_por_item'],
                'preco_a_partir_de': item.get('preco'),
                'preco_da_composicao_padrao': item.get('preco_padrao'),
                'minis': [{'pi_id': s['pi_id'], 'nome': s['nome'],
                           'preco': s['preco'], 'padrao': s['padrao']}
                          for s in menu.get('slots') or []],
            }
            d.pop('itens', None)
    return res


def _executar(nome, inp, *, conversa_id):
    from app.services import carrinho_link, frete
    try:
        if nome == 'consultar_produtos':
            return consultar_produtos(inp.get('busca') or '')
        if nome == 'consultar_frete':
            return frete.consultar_frete(inp.get('endereco_ou_cep') or '',
                                         canal=frete.CANAL_PREVIEW)
        if nome == 'montar_link_carrinho':
            try:
                return carrinho_link.criar(inp.get('itens') or [],
                                           cartinha=inp.get('cartinha'),
                                           conversa_id=conversa_id)
            except carrinho_link.LinkInvalido as exc:
                return {'erro': str(exc)}
        return {'erro': f'ferramenta desconhecida: {nome}'}
    except Exception as exc:  # noqa: BLE001
        logger.exception('atendimento_venda: ferramenta %s falhou', nome)
        # Sem isto a transação abortada (Postgres) derrubava o envio e o
        # salvar do turno — cliente sem resposta (revisão 07/10/2026).
        from app.extensions import db
        db.session.rollback()
        return {'erro': 'falha interna', 'detalhe': type(exc).__name__}


# ── Links permitidos na resposta ───────────────────────────────────────

# Endereço com OU sem esquema ("www.x.com", "bit.ly/x"): o modelo manipulado
# não precisa escrever "https://" para mandar o cliente a outro lugar.
_RE_ENDERECO = re.compile(
    r'(?i)(?:https?://[^\s<>"\'`*\]\[()]+|'
    r'(?:[a-z0-9-]+\.)+(?:com|net|org|br|online|io|me|ly|app|link|site|shop|'
    r'store|info|co|xyz|top|gg|tv|to|biz|club|digital|page|dev|pix)\b'
    r'(?::\d+)?(?:/[^\s<>"\'`*\]\[()]*)?)')
# Marcadores de formatação do WhatsApp/markdown e pontuação colada.
_BORDA = '.,;:!?)]}*_~`>\'"'


def _normalizar_url(u):
    u = (u or '').strip().strip(_BORDA)
    u = re.sub(r'(?i)^https?://', '', u)
    return u.rstrip('/').lower()


def _host(u):
    return _normalizar_url(u).split('/', 1)[0].split(':', 1)[0]


def _urls_das_ferramentas(resultado, acc):
    if isinstance(resultado, dict):
        for k, v in resultado.items():
            if k == 'url' and isinstance(v, str):
                acc.add(_normalizar_url(v))
            else:
                _urls_das_ferramentas(v, acc)
    elif isinstance(resultado, list):
        for v in resultado:
            _urls_das_ferramentas(v, acc)


def _urls_do_historico(historico):
    """Links que o próprio bot já mandou nesta conversa (turnos anteriores)
    podem ser repetidos."""
    acc = set()
    for m in historico or []:
        if m.get('role') == 'assistant' and isinstance(m.get('content'), str):
            acc.update(_normalizar_url(u)
                       for u in _RE_ENDERECO.findall(m['content']))
    return acc


def _links_inventados(texto, permitidos, *, host_loja=''):
    """Endereços do texto que não vieram de ferramenta. O domínio da loja
    sozinho (ex.: "opao.online", e-mail @opao.online) é permitido; caminho
    dentro dele só se a ferramenta devolveu aquele link."""
    out = []
    for bruto in _RE_ENDERECO.findall(texto or ''):
        u = _normalizar_url(bruto)
        if not u or u in permitidos:
            continue
        if host_loja and u == host_loja:
            continue
        if '@' in (texto or '') and ('@' + u) in (texto or '').lower():
            if _host(u) == host_loja:
                continue          # e-mail da padaria
        out.append(bruto)
    return out


# Texto que promete a equipe sem chamar a ferramenta: vira passagem de
# verdade (senão a conversa ficaria no bot e ninguém seria acionado).
_RE_PROMETE_EQUIPE = re.compile(
    r'(?i)\b(?:vou|irei|vamos|j[aá]\s+vou)\s+(?:te\s+|lhe\s+)?'
    r'(?:passar|transferir|encaminhar|chamar)\b|'
    r'\b(?:um|uma|nossa|nosso)\s+(?:atendente|equipe|time)\s+'
    r'(?:vai|ir[aá]|j[aá]\s+vai)\b')


# ── Travas antes do modelo ─────────────────────────────────────────────

def motivo_para_equipe(texto):
    """Motivo (str) quando a última fala do cliente nunca deve ir ao modelo
    de vendas; None quando pode. Reusa as MESMAS regras do motor antigo."""
    from app.services import chatbot
    if not isinstance(texto, str) or not texto.strip():
        return None
    if chatbot._detectar_injection(texto):
        return 'venda: tentativa de manipular o atendimento'
    if chatbot._quer_humano(texto):
        return 'venda: cliente pediu atendente'
    if chatbot.falha_operacional(texto):
        return 'venda: cliente relatou problema com pedido ou entrega'
    if chatbot._reclamacao_aberta(texto):
        return 'venda: reclamação do cliente'
    if _RE_ALERGIA.search(texto):
        return 'venda: alergia ou restrição alimentar'
    return None


# Alergia e restrição alimentar ficam com a equipe (atendimento restrito de
# 24/09/2026): ingrediente errado aqui é risco de saúde, não de venda.
_RE_ALERGIA = re.compile(
    r'(?i)\b(?:al[eé]rgi\w*|intoler\w*|cel[ií]ac\w*|gl[uú]ten|lactose|'
    r'anafila\w*|restri[çc][aã]o\s+aliment\w*)')


def _equipe_falou_na_conversa(conversa_id):
    """A equipe já respondeu em público nesta conversa? O store do bot não
    guarda autoria; o Chatwoot guarda. Falha de leitura = não bloqueia (o
    gateway e a fila seguem protegendo)."""
    if not conversa_id:
        return False
    from app.services import chatwoot
    try:
        if not chatwoot.disponivel():
            return False
        hist = chatwoot.buscar_historico(conversa_id, incluir_autoria=True)
    except Exception:  # noqa: BLE001
        logger.exception('atendimento_venda: autoria indisponível conv=%s',
                         conversa_id)
        return False
    return any(m.get('role') == 'assistant' and m.get('humano') is True
               for m in hist or [])


# ── Turno ──────────────────────────────────────────────────────────────

def responder(historico, *, conversa_id=None):
    """Um turno de venda. Devolve o resultado no formato do atendimento
    restrito (`acao` responder|handoff, `texto`, `motivo`, `tools_usadas`,
    `politica_atendimento`). Nunca levanta."""
    from app.services import atendimento_restrito as restrito
    from app.services import chatbot

    def _equipe(motivo, tools=None):
        out = restrito._encaminhar(motivo)
        out['tools_usadas'] = list(tools or [])
        return out

    ultimo = next((m for m in reversed(historico or [])
                   if m.get('role') == 'user' and not m.get('herdada')), None)
    motivo = motivo_para_equipe((ultimo or {}).get('content'))
    if motivo:
        return _equipe(motivo)
    if _equipe_falou_na_conversa(conversa_id):
        return _equipe('venda: a equipe já está nesta conversa')

    api_key = (os.environ.get('ANTHROPIC_API_KEY')
               or current_app.config.get('ANTHROPIC_API_KEY'))
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key,
                                     timeout=chatbot.API_TIMEOUT_S,
                                     max_retries=1)
    except Exception:  # noqa: BLE001
        logger.exception('atendimento_venda: cliente da API indisponível')
        return _equipe('venda: IA indisponível')

    messages = chatbot._build_messages(historico)
    if not messages:
        return _equipe('venda: sem mensagem')
    tools = [dict(t) for t in TOOLS]
    tools[-1] = {**tools[-1], 'cache_control': {'type': 'ephemeral'}}
    from app.services.loja_checkout import CARTINHA_MAX_CHARS
    system = [{'type': 'text',
               'text': (PROMPT.format(cartinha_max=CARTINHA_MAX_CHARS)
                        + chatbot._horarios_especiais_texto()),
               'cache_control': {'type': 'ephemeral'}}]
    permitidos = _urls_do_historico(historico)
    host_loja = _host(current_app.config.get('LOJA_BASE_URL')
                      or 'https://opao.online')
    tools_usadas = []
    max_tokens = MAX_TOKENS

    for _ in range(MAX_ITERACOES):
        try:
            resp = chatbot._chamar_com_retry_sobrecarga(
                client, ferramentas=True, model=chatbot.MODELO,
                max_tokens=max_tokens, system=system, tools=tools,
                messages=messages)
        except Exception:  # noqa: BLE001
            logger.exception('atendimento_venda: erro na API')
            return _equipe('venda: falha da IA', tools_usadas)
        categoria = ia_modelos.recusa(resp)
        if categoria:
            return _equipe(f'venda: recusa da IA ({categoria})', tools_usadas)
        if getattr(resp, 'stop_reason', None) == 'max_tokens':
            if max_tokens < MAX_TOKENS_RETRY:
                max_tokens = MAX_TOKENS_RETRY
                continue
            return _equipe('venda: resposta cortada', tools_usadas)

        usos = [b for b in resp.content if getattr(b, 'type', '') == 'tool_use']
        if not usos:
            texto = '\n'.join(ia_modelos.textos_visiveis(resp)).strip()
            if not texto:
                return _equipe('venda: resposta vazia', tools_usadas)
            if _RE_PROMETE_EQUIPE.search(texto):
                return _equipe('venda: o bot prometeu a equipe sem '
                               'transferir', tools_usadas)
            inventados = _links_inventados(texto, permitidos,
                                           host_loja=host_loja)
            if inventados:
                logger.error('atendimento_venda: link fora das ferramentas %s '
                             'conv=%s', inventados, conversa_id)
                return _equipe('venda: resposta com link não gerado pelo '
                               'sistema', tools_usadas)
            return {'acao': 'responder', 'texto': texto,
                    'motivo': 'venda pelo WhatsApp',
                    'tools_usadas': tools_usadas,
                    'politica_atendimento': restrito.POLITICA}

        handoff = next((b for b in usos
                        if b.name == 'transferir_para_humano'), None)
        if handoff:
            mot = str((handoff.input or {}).get('motivo') or '').strip()
            return _equipe(f'venda: {mot or "transferida pelo bot"}',
                           tools_usadas + ['transferir_para_humano'])

        messages.append({'role': 'assistant', 'content': resp.content})
        resultados = []
        for b in usos:
            tools_usadas.append(b.name)
            res = _executar(b.name, b.input or {}, conversa_id=conversa_id)
            _urls_das_ferramentas(res, permitidos)
            resultados.append({'type': 'tool_result', 'tool_use_id': b.id,
                               'content': json.dumps(res, ensure_ascii=False,
                                                     default=str)})
        messages.append({'role': 'user', 'content': resultados})

    return _equipe('venda: limite de passos', tools_usadas)

"""Vigia do chatbot — IA supervisora que assiste cada conversa do bot e alerta
o dono via WhatsApp (Z-API) quando detecta problema.

Roda DEPOIS de o bot ter respondido (nao atrasa o cliente). Modelo: Sonnet
4.6 (subido de Haiku em 25/06/2026 na padronizacao do dono — input 3x mais
caro; e o MAIOR volume de IA do sistema porque roda a cada resposta). Por
isso ha um short-circuit deterministico: fechamento trivial do cliente
("ok", "obrigada") nao gasta chamada de modelo (02/07/2026).

Detecta principalmente:
- Cliente irritado/frustrado/prestes a desistir
- Bot afirmando "esgotado"/"nao tem" produto que esta DISPONIVEL NO SITE
  (passamos o catalogo do SITE — loja_catalogo/opao.online, MESMA FONTE que
  o bot consulta; comparar com estoque das lojas fisicas seria
  apples-to-oranges. Ex. historico 12/06/2026, Pain au Chocolat: site
  disponivel, loja fisica 872 un — o bot estava certo pela fonte dele)
- Handoff feito quando o bot poderia ter resolvido
- Possivel perda de venda
- Bot afirmando algo claramente errado (preco estranho, info inventada)

Anti-spam: so dispara WhatsApp se gravidade for `alta` ou `media`. Casos
`baixa`/sem alerta ficam so no log e no historico em memoria (rota
/admin/vigia/diag mostra os ultimos).
"""
import json
import logging
import os
import re
from collections import deque

from flask import current_app

logger = logging.getLogger(__name__)

MODELO = 'claude-sonnet-5'
MAX_TOKENS = 400
# Historico em memoria das ultimas avaliacoes (volatil, reinicia no deploy).
# Bom o suficiente pra confirmar "ta rodando?" — pra historico durador usar
# AuditLog ou tabela dedicada no futuro.
HISTORICO_MAX = 30
_historico = deque(maxlen=HISTORICO_MAX)
# Conversas ja avisadas como abandono nesta sessao (anti-spam do detector
# de abandono). Volatil — reseta no deploy, o que e aceitavel: vale a pena
# reavisar depois de um restart se o cliente ainda nao voltou.
_avisados_abandono = set()

PROMPT_VIGIA = """Você é o Vigia: supervisor automático do bot de atendimento da O Pão (padaria artesanal).
Lê a conversa abaixo e classifica a gravidade. SÓ gravidade=alta vira aviso na
hora no WhatsApp do dono; media entra num resumo diário (não incomoda na hora).

GRAVIDADE=ALTA (urgente — o dono precisa saber AGORA):
- Cliente IRRITADO, agressivo, SURTANDO/alterado, ofendido, ou prestes a desistir/cancelar
- Cliente reclamando de algo sério (pedido errado, atraso, cobrança indevida)
- Bot afirmou "esgotado"/"não temos" para item que aparece como DISPONÍVEL=true no catálogo do site abaixo (ERRO REAL — o bot e o cliente compram pelo SITE; estoque de loja física é OUTRA fonte e NÃO deve ser usado pra contradizer o bot)
  ⚠️ ATENÇÃO À DATA: disponibilidade é POR DATA DE ENTREGA. Se o item traz
  "INDISPONIVEL para entrega em: DD/MM" e a conversa é sobre ESSA data, o bot
  dizendo "não temos para DD/MM" está CERTO — NÃO alerte (falso positivo real
  na véspera do Dia dos Pais: só cestas à venda pra 09/08 e o item DISPONÍVEL
  no geral). Só é erro quando a data da conversa NÃO está listada e o item
  está DISPONÍVEL (ou quando ele nega o item sem data nenhuma envolvida).
- Bot disse algo claramente errado: preço estranho, prazo errado, info inventada, contradição grave
- PERDA DE VENDA clara: cliente estava comprando, o bot atrapalhou/confundiu, e o cliente saiu
- HANDOFF PREGUIÇOSO EM VENDA: o bot transferiu pro humano SEM usar nenhuma ferramenta
  (veja "FERRAMENTAS USADAS" abaixo — lista vazia) E o cliente estava COMPRANDO
  (perguntou de produto/cesta/preço/disponibilidade, pediu link, montou pedido).
  Caso real 12/06/2026: "tem cesta de café? entrega amanhã?" → bot transferiu
  sem nem consultar o catálogo. Cliente comprando + bot que nem tentou = venda
  em risco AGORA.
- HANDOFF PREGUIÇOSO EM CONSULTA DE PEDIDO: o bot transferiu SEM usar
  consultar_pedido quando o cliente forneceu (ou disse ter) número do pedido e
  perguntou sobre status/rastreio/data de entrega. O bot TEM a ferramenta
  consultar_pedido — usá-la era a primeira coisa a fazer.

  ⚠️ NÃO é handoff preguiçoso (NÃO alerte por isso) quando:
  - Cliente reclama que pedido "não chegou", "atrasou", "veio errado", "veio
    quebrado": handoff PRA HUMANO É CERTO — é caso operacional que humano
    resolve melhor que bot. Pode alertar como reclamação séria (regra acima),
    mas NÃO como "handoff preguiçoso".
  - Cliente pediu NF/nota fiscal e bot pediu CPF+número: NÃO é preguiçoso, é
    o protocolo correto (precisa dos dois).
  - Cliente PEDIU explicitamente humano ("quero falar com atendente"): NÃO
    alerte, handoff foi o que ele pediu.
  - Cliente fez reclamação de problema sério (cobrança indevida, atendimento
    da loja): handoff é correto.

GRAVIDADE=MEDIA (não urgente — vai pro resumo diário):
- Handoff que o bot PODERIA ter resolvido (ex: "o que tem na cesta?", dúvida simples de produto) — exceto o caso ALTA acima (sem ferramenta + cliente comprando)
- Bot deu resposta truncada/confusa/repetiu saudação, mas sem erro grave
- Cliente meio perdido depois de várias trocas

SEM ALERTA (alerta=false):
- Conversa fluindo, cliente satisfeito ou neutro
- Handoff CORRETO (entrega/CEP/frete/reagendar pedido, pedido de humano)
- Cliente só tirou dúvida e foi atendido

Em dúvida entre alta e media, escolha media (só o urgente de verdade é alta).

Responda APENAS com JSON válido neste formato:
{"alerta": true|false, "gravidade": "alta"|"media"|null, "motivo": "frase curta em PT-BR", "acao_sugerida": "frase curta ou vazia"}

NUNCA inclua texto fora do JSON."""


def disponivel():
    cfg = current_app.config
    return bool(cfg.get('CHATBOT_VIGIA')
                and (os.environ.get('ANTHROPIC_API_KEY') or cfg.get('ANTHROPIC_API_KEY')))


def _numero_destino():
    cfg = current_app.config
    return ((cfg.get('CHATBOT_VIGIA_NUMERO') or '').strip()
            or (cfg.get('ZAPI_NUMERO_DESTINO') or '').strip())


def _resumo_catalogo_site(limite=120):
    """Catalogo do SITE (opao.online) — MESMA fonte que o bot consulta via
    `consultar_produtos` (estoque REAL da loja do site). Cada linha = nome +
    estado (DISPONIVEL ou ESGOTADO). E so essa fonte que o vigia pode usar pra
    contradizer o bot quando ele diz 'esgotado' — estoque de loja fisica e
    outra realidade (venda balcao) e gerava falso alerta (caso real
    12/06/2026: Pain au Chocolat 872 un nas lojas mas site disponivel — o
    vigia avisou 'erro critico' quando bot e site estavam alinhados).

    Lista TODOS os produtos do catalogo (disponiveis e esgotados) pro
    Haiku ter contexto pra distinguir os dois casos."""
    try:
        from app.services import bot_tools
    except Exception:  # noqa: BLE001
        return ''
    try:
        catalogo = bot_tools.catalogo_disponibilidade()
    except Exception:  # noqa: BLE001
        logger.exception('vigia: _resumo_catalogo_site falhou')
        return ''
    if not catalogo:
        return '(catalogo do site indisponivel agora)'
    # Dedup por nome (mesma fonte do bot): disponibilidade soma (OR); as
    # datas indisponiveis se INTERSECTAM — homonimo (receita+produto) conta
    # como indisponivel numa data so se TODAS as entradas estiverem.
    estado = {}
    for p in catalogo:
        nome = (p.get('nome') or '').strip()
        if not nome:
            continue
        disp = bool(p.get('disponivel'))
        datas = set(p.get('indisponivel_em') or [])
        if nome in estado:
            d0, s0 = estado[nome]
            estado[nome] = (d0 or disp, s0 & datas)
        else:
            estado[nome] = (disp, datas)
    itens = sorted(estado.items())[:limite]
    linhas = []
    for nome, (disp, datas) in itens:
        extra = ''
        if datas:
            ordenadas = sorted(datas, key=lambda s: (s[3:], s[:2]))
            extra = ' — INDISPONIVEL para entrega em: ' + ', '.join(ordenadas)
        linhas.append(
            f'- {nome}: {"DISPONIVEL" if disp else "ESGOTADO"}{extra}')
    return '\n'.join(linhas)


def _formatar_historico(historico):
    linhas = []
    for m in (historico or [])[-12:]:
        role = m.get('role')
        content = (m.get('content') or '').strip()
        if not content:
            continue
        prefixo = 'CLIENTE' if role == 'user' else 'BOT'
        linhas.append(f'{prefixo}: {content}')
    return '\n'.join(linhas)


def _chamar_modelo(api_key, contexto):
    import anthropic
    # timeout: vigia roda em thread best-effort — nunca vale segurar 10min.
    client = anthropic.Anthropic(api_key=api_key, timeout=45, max_retries=1)
    resp = client.messages.create(
        model=MODELO,
        max_tokens=MAX_TOKENS,
        # Sonnet 5 liga thinking adaptativo por padrao; o vigia e o MAIOR
        # volume de IA do sistema e devolve JSON curto — thinking aqui
        # comeria o teto de 400 tokens e multiplicaria o custo. Desligado.
        thinking={'type': 'disabled'},
        # cache_control: o PROMPT_VIGIA e estatico e o vigia e o maior volume
        # de IA do sistema — cache read custa 0.1x do input.
        system=[{'type': 'text', 'text': PROMPT_VIGIA,
                 'cache_control': {'type': 'ephemeral'}}],
        messages=[{'role': 'user', 'content': contexto}],
    )
    from app.services import uso_ia
    uso_ia.registrar('vigia', MODELO, getattr(resp, 'usage', None))
    texto = ''.join(b.text for b in resp.content
                    if getattr(b, 'type', None) == 'text' and b.text).strip()
    # Tolerante a markdown wrappers do tipo ```json ... ```
    if texto.startswith('```'):
        texto = texto.split('```', 2)[1]
        if texto.startswith('json'):
            texto = texto[4:].strip()
        texto = texto.rsplit('```', 1)[0].strip()
    return json.loads(texto)


def _avaliar_interno(historico, *, conv_id=None, nome_contato='', resultado_bot=None):
    """Logica de avaliar — multiplos returns, sem efeitos colaterais alem
    do envio via Z-API. O wrapper `avaliar` cuida do registro no historico."""
    if not disponivel():
        return {'pulou': 'vigia desligado'}

    rb = resultado_bot or {}

    # ── DETECTOR DETERMINISTICO: HANDOFF PREGUIÇOSO EM VENDA ──────────
    # Pedido do dono 16/06/2026 (auditor reportou caso Ale, venda perdida):
    # quando o bot transfere SEM ter chamado tool de busca/resolucao E a
    # conversa tem sinais claros de COMPRA EM CURSO, alerta IMEDIATO
    # (banner + WhatsApp), nao espera o resumo diario. Determinístico
    # (regex) — auditavel, sem depender do humor do Haiku que ja falhou
    # em pegar isso (caso real: conversas de hoje).
    #
    # Quando bate, pula o Haiku (1) reage instantaneo, (2) evita o Haiku
    # subestimar como "media" e o alerta nao sair. O motivo customizado
    # diz exatamente o que aconteceu pra o dono agir.
    if _e_handoff_preguicoso_em_compra(historico, rb, conv_id=conv_id):
        logger.warning('vigia: HANDOFF PREGUICOSO EM VENDA detectado '
                       'conv=%s (deterministico, pulou Haiku)', conv_id)
        veredicto = {
            'alerta': True,
            'gravidade': 'alta',
            'motivo': ('🚨 Venda em risco: bot fez handoff sem tentar '
                       'resolver. Cliente estava comprando e foi empurrado '
                       'pra fila sem o bot chamar uma ferramenta sequer.'),
            'acao_sugerida': ('Abrir a conversa AGORA, antes do cliente '
                              'esfriar — ainda dá pra reverter.'),
        }
        return _processar_veredicto(veredicto, nome_contato, conv_id)

    # ── SHORT-CIRCUIT DE CUSTO (02/07/2026) ────────────────────────────
    # "ok" / "obrigada" / "valeu" depois de o bot responder nao tem o que
    # auditar — e o vigia e o MAIOR volume de IA do sistema (roda em TODA
    # resposta, com Sonnet). Fechamento trivial nao gasta modelo. Handoff
    # nunca cai aqui (sempre avalia).
    ultima_cliente = next((m.get('content') or ''
                           for m in reversed(historico or [])
                           if (m or {}).get('role') == 'user'), '')
    if rb.get('acao') != 'handoff' and _e_fechamento(ultima_cliente):
        logger.info('vigia: short-circuit fechamento trivial conv=%s', conv_id)
        return {'pulou': 'fechamento trivial'}

    api_key = (os.environ.get('ANTHROPIC_API_KEY')
               or current_app.config.get('ANTHROPIC_API_KEY'))
    if not api_key:
        return {'pulou': 'sem ANTHROPIC_API_KEY'}

    try:
        # Sinal pro caso HANDOFF PREGUICOSO do prompt: lista das tools
        # que o bot usou neste turno. Vazia + handoff + cliente comprando
        # = ALTA (caso real 12/06/2026, conv #198).
        tools_usadas = rb.get('tools_usadas')
        if tools_usadas is None:
            tools_txt = '(desconhecido — versao antiga do bot)'
        elif tools_usadas:
            tools_txt = ', '.join(tools_usadas)
        else:
            tools_txt = 'NENHUMA'
        contexto = (
            f'Cliente: {nome_contato or "(sem nome)"}\n'
            f'Conversation ID: {conv_id or "?"}\n\n'
            f'CONVERSA (últimas mensagens):\n{_formatar_historico(historico)}\n\n'
            f'ÚLTIMA AÇÃO DO BOT: {rb.get("acao", "?")} - '
            f'{rb.get("motivo", "")}\n'
            f'FERRAMENTAS USADAS PELO BOT NESTE TURNO: {tools_txt}\n\n'
            f'CATALOGO DO SITE (mesma fonte que o bot usa — '
            f'CONTRADIGA o bot SO se ele disser esgotado pra item '
            f'marcado DISPONIVEL aqui; "INDISPONIVEL para entrega em: '
            f'DD/MM" e a verdade POR DATA — bot negando o item PRA ESSA '
            f'data esta CERTO):\n'
            f'{_resumo_catalogo_site()}'
        )
        veredicto = _chamar_modelo(api_key, contexto)
    except Exception as exc:  # noqa: BLE001
        logger.exception('vigia: avaliacao falhou')
        return {'erro': str(exc)}

    return _processar_veredicto(veredicto, nome_contato, conv_id)


_DEDUP_ALTA_HORAS = 2


def _alerta_alta_recente(conv_id, horas=_DEDUP_ALTA_HORAS):
    """True se esta conversa JA disparou WhatsApp ALTA dentro da janela —
    evita metralhar o dono em turnos ruins consecutivos da mesma conversa.
    Fail-open (erro na consulta = alerta sai): perder alerta e pior que
    alertar duas vezes."""
    if not conv_id:
        return False
    try:
        from datetime import timedelta

        from app.models import VigiaVeredito
        from app.utils import agora
        corte = agora() - timedelta(hours=horas)
        return (VigiaVeredito.query
                .filter(VigiaVeredito.conv_id == str(conv_id),
                        VigiaVeredito.criado_em >= corte,
                        VigiaVeredito.gravidade == 'alta',
                        VigiaVeredito.alerta.is_(True),
                        VigiaVeredito.enviado_whatsapp.is_(True))
                .first()) is not None
    except Exception:  # noqa: BLE001
        logger.exception('vigia: dedup ALTA falhou (fail-open)')
        return False


def handoff_foi_preguicoso(tools_usadas, conv_id=None):
    """Regra UNICA de 'handoff preguicoso': transferiu sem ter chamado
    NENHUMA tool de leitura antes. Compartilhada entre o detector do vigia
    e o agregador do auditor. `tools_usadas` None (bot antigo) = False.

    07/07/2026 (achado do dono no resumo do auditor): a lista e POR TURNO —
    bot que consultou o pedido num turno e transferiu no SEGUINTE saia como
    "preguicoso" com lista vazia. Com `conv_id`, olhamos tambem as tools dos
    turnos recentes (2h) da MESMA conversa antes de acusar."""
    if tools_usadas is None:
        return False

    def _tem_leitura(lst):
        return any(t for t in (lst or [])
                   if t and t not in ('transferir_para_humano',
                                      'encerrar_conversa'))

    if _tem_leitura(tools_usadas):
        return False
    if conv_id:
        import json as _json
        from datetime import timedelta

        from app.models import VigiaVeredito
        from app.utils import agora
        corte = agora() - timedelta(hours=2)
        for v in (VigiaVeredito.query
                  .filter(VigiaVeredito.conv_id == str(conv_id),
                          VigiaVeredito.criado_em >= corte,
                          VigiaVeredito.tools_usadas.isnot(None))
                  .order_by(VigiaVeredito.criado_em.desc()).limit(10).all()):
            try:
                if _tem_leitura(_json.loads(v.tools_usadas)):
                    return False       # trabalhou em turno anterior da conversa
            except (TypeError, ValueError):
                continue
    return True


def _processar_veredicto(veredicto, nome_contato, conv_id):
    """Pos-processa um veredicto (vem do Haiku OU do detector deterministico):
    valida, decide se manda WhatsApp, dispara o envio. Centralizado pra os
    dois caminhos gerarem o MESMO efeito (banner do painel + WhatsApp +
    persistencia via _registrar)."""
    if not isinstance(veredicto, dict):
        return {'erro': 'veredicto invalido'}

    logger.info('vigia conv=%s veredicto=%s', conv_id, veredicto)

    # SO alta dispara WhatsApp na hora. media fica registrado (resumo diario),
    # sem incomodar o dono em tempo real.
    if not veredicto.get('alerta') or veredicto.get('gravidade') != 'alta':
        return {'silencio': True, 'veredicto': veredicto}

    # Dedup (02/07/2026): dois turnos ALTA seguidos da MESMA conversa
    # mandavam dois WhatsApps. Ja alertou esta conv na janela → registra o
    # veredito (banner do painel segue vivo) mas nao re-envia.
    if _alerta_alta_recente(conv_id):
        logger.info('vigia: WhatsApp ALTA suprimido (dedup) conv=%s', conv_id)
        return {'silencio': 'dedup-alta', 'veredicto': veredicto}

    numero = _numero_destino()
    if not numero:
        logger.warning('vigia: alerta gerado mas sem CHATBOT_VIGIA_NUMERO/ZAPI_NUMERO_DESTINO')
        return {'erro': 'sem destino', 'veredicto': veredicto}

    mensagem = _montar_mensagem(veredicto, nome_contato, conv_id)
    try:
        from app.services import zapi
        envio = zapi.enviar_texto(numero, mensagem)
    except Exception as exc:  # noqa: BLE001
        logger.exception('vigia: envio Z-API falhou')
        return {'erro': f'zapi: {exc}', 'veredicto': veredicto}

    return {'enviado': bool(envio.get('ok')), 'envio': envio, 'veredicto': veredicto}


# ── Detector deterministico: handoff preguicoso em VENDA ─────────────
#
# Auditor reportou caso real 16/06/2026 (Ale, venda perdida): bot transferiu
# sem chamar nenhuma tool, no meio de uma compra. Esse padrao tem 3 sinais
# combinados (todos precisam bater):
#   1. acao=handoff
#   2. tools_usadas vazia OU so ['transferir_para_humano']
#   3. historico recente tem sinais fortes de COMPRA EM CURSO
#      E nao tem sinais fortes de RECLAMACAO (handoff legitimo)
#
# Sinais conservadores — falso positivo gera ruido no WhatsApp do dono, mas
# falso negativo perde venda. Preferimos errar pra MAIS alerta.

_SINAIS_COMPRA = re.compile(
    r'\b('
    r'compr(ar|ei|ando|a)|pag(ar|amento|amento|uei|o|a)|finaliz(ar|ando|ei)|'
    r'fechar( o)?( meu)?( a)? (pedido|compra)|checkout|carrinho|link|'
    r'cesta|cestas|kit|box|brunch|sourdough|croissant|brioche|granola|'
    r'pre[çc]o|quanto (custa|fica|sai)|valor|fazer( um)? pedido|'
    r'quero pedir|quero comprar|tem .* dispon|quero a |gostaria de '
    r')',
    re.IGNORECASE,
)

# Reclamacao = handoff humano e correto (nao alerta como preguicoso).
_SINAIS_RECLAMACAO = re.compile(
    r'('
    # 26/07/2026 (caso Gabriela, conv 918): o cliente escreveu "as visitas
    # estavam esperando e nao chegava nunca" + "eu acabei cancelando" e
    # NENHUMA alternativa casava — 'chegou' nao cobre 'chegava/chegaram' e
    # 'cancelar meu pedido' nao cobre 'cancelei/cancelando'. Sem casar aqui,
    # uma reclamacao de venda PERDIDA era classificada como fechamento banal.
    r'\bn[aã]o cheg(ou|aram|ava|avam)|\bnunca cheg\w*|'
    r'\bcancelei\b|\bacabei cancelando\b|'
    r'\bn[aã]o (recebi|veio)|\batras(ou|ado|ando|o)|'
    r'\bveio (errado|quebrado|estragado|diferente|faltando|amassado|'
    r'murcho|seco|menor|assim)|'
    r'\breembolso|\breclamar|\breclama[cç][aã]o|\bcancelar (meu )?pedido|'
    r'\bdevolver|\btrocar|\bt[aá] estragado|\bt[aá] quebrado|\bp[eé]ssim\w*|'
    r'\bfalar com (gerente|dono|respons[aá]vel)|'
    # Qualidade/tamanho do produto JÁ recebido (caso 23/06/2026: croissant
    # "tão pequenininho", "todos estão assim?") — reclamação pós-venda, o
    # handoff humano é CORRETO, não "venda perdida".
    r'\bpequen\w*|\bmin[uú]scul\w*|\btamanho|\bmenor\b|'
    r'\bmurch\w*|\bressecad\w*|\bqueimad\w*|\bmofad\w*|\bazed\w*|'
    r'\bestragad\w*|\bestranho|\bhorr[ií]vel|\bqualidade|'
    r'\bdur[oa]\b|\bsec[oa]\b|\bcru[a]?\b|\bruim\b|'
    r'\btodos? (est[aã]o|s[aã]o|v[eê]m|vem) assim|\bveio assim|\bvieram assim'
    r')',
    re.IGNORECASE,
)


def _e_handoff_preguicoso_em_compra(historico, resultado_bot, conv_id=None):
    """True se: bot fez handoff SEM tool de busca + cliente em compra ativa.
    Determinístico pra ser auditavel e nao depender do Haiku."""
    rb = resultado_bot or {}
    if rb.get('acao') != 'handoff':
        return False
    if rb.get('tools_usadas') is None:
        return False  # versao antiga do bot — sem sinal confiavel
    if not handoff_foi_preguicoso(rb.get('tools_usadas'), conv_id=conv_id):
        return False  # bot tentou algo — handoff legitimo

    # Junta as ultimas msgs do cliente (texto, role=user)
    msgs_user = [m.get('content', '') for m in (historico or [])[-12:]
                 if m.get('role') == 'user' and m.get('content')]
    if not msgs_user:
        return False
    texto = ' '.join(msgs_user).lower()
    if _SINAIS_RECLAMACAO.search(texto):
        return False  # reclamacao = handoff humano correto, nao preguicoso
    # Cliente PEDIU humano explicitamente = handoff legitimo (excecao do proprio
    # prompt do bot), mesmo com tools=[]. Sem isso, dava falso positivo: quem
    # via cestas e dizia "quero falar com atendente" virava "preguicoso".
    # Reusa o MESMO regex do bot (chatbot._quer_humano) pra nao divergir.
    try:
        from app.services.chatbot import _quer_humano
        if any(_quer_humano(m) for m in msgs_user):
            return False
    except Exception:  # noqa: BLE001
        pass
    return bool(_SINAIS_COMPRA.search(texto))


def _montar_mensagem(veredicto, nome_contato, conv_id):
    cfg = current_app.config
    base_cw = (cfg.get('CHATWOOT_URL') or '').rstrip('/')
    acc = (cfg.get('CHATWOOT_ACCOUNT_ID') or '').strip()
    link = (f'{base_cw}/app/accounts/{acc}/conversations/{conv_id}'
            if base_cw and acc and conv_id else '')

    grav = (veredicto.get('gravidade') or '').upper()
    motivo = (veredicto.get('motivo') or '').strip()
    acao = (veredicto.get('acao_sugerida') or '').strip()

    linhas = [f'*Vigia do bot* [{grav}]']
    if nome_contato:
        linhas.append(f'Cliente: {nome_contato}')
    if motivo:
        linhas.append('')
        linhas.append(motivo)
    if acao:
        linhas.append('')
        linhas.append(f'Sugestão: {acao}')
    if link:
        linhas.append('')
        linhas.append(link)
    return '\n'.join(linhas)


def _registrar(resultado, conv_id, nome_contato, ultima_mensagem_cliente,
               resultado_bot=None):
    """Adiciona o resultado ao historico em memoria (`_historico`) E persiste
    em VigiaVeredito (pro auditor diario achar padroes). Persistencia e
    best-effort: nunca propaga erro."""
    import time as _time

    from app.utils import agora as _ag

    veredicto = (resultado or {}).get('veredicto') or {}
    _historico.append({
        'epoch': _time.time(),
        'ts': _ag().strftime('%d/%m %H:%M'),
        'conv_id': conv_id,
        'cliente': nome_contato or '',
        'mensagem_cliente': (ultima_mensagem_cliente or '')[:200],
        'alerta': bool(veredicto.get('alerta')),
        'gravidade': veredicto.get('gravidade'),
        'motivo': veredicto.get('motivo', ''),
        'enviado': bool(resultado.get('enviado')),
        'pulou': resultado.get('pulou'),
        'erro': resultado.get('erro'),
    })
    # Persiste (best-effort).
    try:
        import json as _json

        from app.extensions import db
        from app.models import VigiaVeredito
        rb = resultado_bot or {}
        tools = rb.get('tools_usadas')
        tools_json = (_json.dumps(list(tools), ensure_ascii=False)
                      if isinstance(tools, (list, tuple))
                      else None)
        db.session.add(VigiaVeredito(
            conv_id=str(conv_id) if conv_id is not None else None,
            cliente=(nome_contato or '')[:200] or None,
            mensagem_cliente=(ultima_mensagem_cliente or '')[:2000] or None,
            bot_acao=rb.get('acao'),
            bot_motivo=(rb.get('motivo') or '')[:500] or None,
            alerta=bool(veredicto.get('alerta')),
            gravidade=veredicto.get('gravidade'),
            motivo_vigia=(veredicto.get('motivo') or '')[:1000] or None,
            enviado_whatsapp=bool(resultado.get('enviado')),
            tools_usadas=tools_json,
        ))
        db.session.commit()
    except Exception:  # noqa: BLE001
        logger.exception('vigia: persistir VigiaVeredito falhou')
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


def avaliar(historico, *, conv_id=None, nome_contato='', resultado_bot=None):
    """Wrapper publico: chama o avaliador e registra o resultado no historico
    em memoria (consumido por /admin/vigia/diag). Best-effort: erros do
    registro nunca afetam o fluxo do bot.

    `historico`: lista [{'role', 'content'}] da conversa
    `resultado_bot`: {'acao', 'texto', 'motivo'?} do que o bot acabou de fazer
    """
    res = _avaliar_interno(historico, conv_id=conv_id,
                            nome_contato=nome_contato,
                            resultado_bot=resultado_bot)
    ultima_msg = ''
    for m in reversed(historico or []):
        if m.get('role') == 'user' and (m.get('content') or '').strip():
            ultima_msg = m['content'].strip()
            break
    try:
        _registrar(res, conv_id, nome_contato, ultima_msg,
                   resultado_bot=resultado_bot)
    except Exception:  # noqa: BLE001
        logger.exception('vigia: registro no historico falhou')
    return res


def ultimos(limite=30):
    """Devolve os ultimos N veredictos (mais recentes primeiro) pro
    /admin/vigia/diag mostrar. Volatil — reseta no deploy."""
    return list(reversed(list(_historico)))[:limite]


def disparar_teste(cenario='estoque'):
    """Dispara uma avaliacao com conversa SINTETICA pra confirmar que o
    pipeline inteiro funciona (Haiku -> Z-API -> WhatsApp do dono). Retorna
    o resultado bruto pra mostrar na rota /admin/vigia/teste.

    Cenarios:
      - 'estoque': bot afirma esgotado pra item que tem nas lojas (ALTA)
      - 'irritado': cliente irritado com atendimento (ALTA)
      - 'silencio': conversa neutra (NAO deve disparar)
    """
    base = {
        'estoque': [
            {'role': 'user', 'content': 'oi, vocês têm croissant de amêndoas?'},
            {'role': 'assistant', 'content': 'Oi! Infelizmente o croissant de '
             'amêndoas está esgotado hoje. 😕'},
            {'role': 'user', 'content': 'mas a vendedora aqui da loja Brooklin '
             'falou que tem'},
        ],
        'irritado': [
            {'role': 'user', 'content': 'queria fazer um pedido pra amanhã'},
            {'role': 'assistant', 'content': 'Posso te ajudar! Qual cesta você '
             'quer?'},
            {'role': 'user', 'content': 'já te falei 3 vezes, é a Family Box. '
             'Vocês não prestam atenção. Vou desistir.'},
        ],
        'silencio': [
            {'role': 'user', 'content': 'oi, qual o horário de vocês hoje?'},
            {'role': 'assistant', 'content': 'Oi! As lojas abrem das 7h às 20h, '
             'todos os dias.'},
            {'role': 'user', 'content': 'obrigada!'},
        ],
    }
    historico = base.get(cenario, base['estoque'])
    return avaliar(historico,
                   conv_id=f'teste-{cenario}',
                   nome_contato='Teste do Vigia',
                   resultado_bot={'acao': 'responder',
                                   'motivo': f'cenario {cenario}'})


PROMPT_ABANDONO = """Você é o Vigia: supervisor automático do bot de atendimento da O Pão (padaria artesanal).
Estou te mostrando uma conversa que está PARADA há um tempo — o cliente não respondeu mais.
Decida se o dono precisa ser AVISADO no WhatsApp pra um humano pegar a conversa.

ALERTE (gravidade=alta) quando:
- Cliente claramente DESISTIU de comprar (estava no meio de um pedido, sumiu)
- Cliente parecia INSATISFEITO/IRRITADO antes de parar de responder
- Bot deu uma resposta CONFUSA ou pediu esclarecimento que travou a conversa
- Possível PERDA DE VENDA (cliente perguntou produto/preço, bot respondeu, cliente sumiu)

ALERTE (gravidade=media) quando:
- Bot pediu esclarecimento e cliente sumiu (potencial dúvida sem resolver)
- Conversa começou interessante (pedido, dúvida concreta) mas parou no meio

NÃO alerte quando:
- Conversa era apenas cumprimento ("oi", "boa tarde") sem demanda concreta
- Cliente já tinha sido atendido (info recebida) — silêncio normal
- Conversa muito curta ou sem contexto util pra um humano

Seja RIGOROSO: alerta demais vira ruído. Em dúvida, NÃO alerte.

Responda APENAS com JSON válido neste formato:
{"alerta": true|false, "gravidade": "alta"|"media"|null, "motivo": "frase curta em PT-BR", "acao_sugerida": "frase curta ou vazia"}

NUNCA inclua texto fora do JSON."""


def _chamar_modelo_abandono(api_key, contexto):
    """Igual ao _chamar_modelo, mas com PROMPT_ABANDONO."""
    import anthropic
    # timeout: roda em cron best-effort — nunca vale segurar 10min.
    client = anthropic.Anthropic(api_key=api_key, timeout=45, max_retries=1)
    resp = client.messages.create(
        model=MODELO,
        max_tokens=MAX_TOKENS,
        thinking={'type': 'disabled'},   # mesma regra da avaliacao acima
        # cache_control: o cron avalia varias conversas paradas em sequencia
        # na mesma janela de 5min — o PROMPT_ABANDONO estatico cacheia.
        system=[{'type': 'text', 'text': PROMPT_ABANDONO,
                 'cache_control': {'type': 'ephemeral'}}],
        messages=[{'role': 'user', 'content': contexto}],
    )
    from app.services import uso_ia
    uso_ia.registrar('vigia', MODELO, getattr(resp, 'usage', None))
    texto = ''.join(b.text for b in resp.content
                    if getattr(b, 'type', None) == 'text' and b.text).strip()
    if texto.startswith('```'):
        texto = texto.split('```', 2)[1]
        if texto.startswith('json'):
            texto = texto[4:].strip()
        texto = texto.rsplit('```', 1)[0].strip()
    return json.loads(texto)


def ja_avisado_abandono(conv_id, horas=24):
    """Dedupe do detector de abandono: memoria (rapido) + BANCO
    (VigiaVeredito — sobrevive a deploy). Caso real 12/06/2026: o set em
    memoria zerava a cada deploy; no dia em que o detector foi curado da
    cegueira do token (nao listava conversas), ele metralhou o dono com
    o backlog inteiro de uma vez — e re-metralharia a cada deploy.
    O avaliar_abandono SEMPRE grava VigiaVeredito com prefixo
    '[ABANDONO' na mensagem (alertando ou silenciando), entao a
    existencia de linha recente = ja avaliado."""
    if conv_id in _avisados_abandono:
        return True
    try:
        from datetime import timedelta

        from app.models import VigiaVeredito
        from app.utils import agora
        corte = agora() - timedelta(hours=horas)
        row = (VigiaVeredito.query
               .filter(VigiaVeredito.conv_id == str(conv_id),
                       VigiaVeredito.criado_em >= corte,
                       VigiaVeredito.mensagem_cliente.like('[ABANDONO%'))
               .first())
        if row:
            _avisados_abandono.add(conv_id)   # aquece o cache
            return True
    except Exception:  # noqa: BLE001
        logger.exception('vigia: dedupe de abandono via banco falhou')
    return False


def avaliar_abandono(historico, *, conv_id=None, nome_contato='', minutos_sem_resposta=0):
    """Avalia conversa PARADA (cliente sumiu) e alerta no WhatsApp se valer.
    Best-effort. Usado pelo cron `_run_vigia_abandono` no seru_cron.py."""
    if not disponivel():
        return {'pulou': 'vigia desligado'}

    # Conversa que e so marcacao de story do IG: nao ha cliente esperando
    # nem venda em risco — nao gasta modelo nem alerta (dono, 06/07/2026).
    ultima_user = next((m for m in reversed(historico or [])
                        if m.get('role') == 'user'
                        and (m.get('content') or '').strip()), None)
    if ultima_user and _e_mencao_story(ultima_user.get('content')):
        return {'pulou': 'mencao de story do Instagram'}

    api_key = (os.environ.get('ANTHROPIC_API_KEY')
               or current_app.config.get('ANTHROPIC_API_KEY'))
    if not api_key:
        return {'pulou': 'sem ANTHROPIC_API_KEY'}

    try:
        contexto = (
            f'Cliente: {nome_contato or "(sem nome)"}\n'
            f'Conversation ID: {conv_id or "?"}\n'
            f'Sem resposta ha {minutos_sem_resposta} minutos.\n\n'
            f'CONVERSA:\n{_formatar_historico(historico)}'
        )
        veredicto = _chamar_modelo_abandono(api_key, contexto)
    except Exception as exc:  # noqa: BLE001
        logger.exception('vigia abandono: avaliacao falhou')
        return {'erro': str(exc)}

    if not isinstance(veredicto, dict):
        return {'erro': 'veredicto invalido'}

    logger.info('vigia abandono conv=%s min=%s veredicto=%s',
                conv_id, minutos_sem_resposta, veredicto)

    # Registra no historico em memoria mesmo nao alertando (visivel no diag).
    ultima_msg = ''
    for m in reversed(historico or []):
        if m.get('role') == 'user' and (m.get('content') or '').strip():
            ultima_msg = m['content'].strip()
            break
    res = {'veredicto': veredicto, 'silencio': not veredicto.get('alerta')}

    # So alta (desistencia clara / perda de venda) dispara na hora; media -> resumo.
    if not veredicto.get('alerta') or veredicto.get('gravidade') != 'alta':
        try:
            _registrar(res, conv_id, nome_contato, f'[ABANDONO {minutos_sem_resposta}min] {ultima_msg}')
        except Exception:  # noqa: BLE001
            logger.exception('vigia abandono: registro falhou')
        return res

    numero = _numero_destino()
    if not numero:
        logger.warning('vigia abandono: alerta gerado mas sem destino')
        return {'erro': 'sem destino', 'veredicto': veredicto}

    # Prefixa motivo com "ABANDONO" pra ficar claro no WhatsApp.
    veredicto_msg = dict(veredicto)
    veredicto_msg['motivo'] = (f'[{minutos_sem_resposta} min sem resposta] '
                                + (veredicto.get('motivo') or '').strip())
    mensagem = _montar_mensagem(veredicto_msg, nome_contato, conv_id)
    try:
        from app.services import zapi
        envio = zapi.enviar_texto(numero, mensagem)
    except Exception as exc:  # noqa: BLE001
        logger.exception('vigia abandono: envio Z-API falhou')
        return {'erro': f'zapi: {exc}', 'veredicto': veredicto}

    res = {'enviado': bool(envio.get('ok')), 'envio': envio, 'veredicto': veredicto}
    try:
        _registrar(res, conv_id, nome_contato, f'[ABANDONO {minutos_sem_resposta}min] {ultima_msg}')
    except Exception:  # noqa: BLE001
        logger.exception('vigia abandono: registro falhou')
    return res


# Encerramentos: "ok", "obrigada", "valeu" — o cliente NÃO está esperando
# resposta (caso 23/06/2026: humano resolveu, cliente respondeu "Ok" e o
# vigia alertou como se alguém precisasse olhar).
# Um "token" de encerramento; a mensagem pode ter vários em sequência
# ("ok obrigada", "valeu mesmo", "tá bom show").
_FECHAMENTO_TOKEN = (
    r'(ok(ay)?|t[aá]|bom|certo|joia|j[oó]ia|blz|beleza|valeu|vlw|'
    r'obrigad[oa]?|obg|brigad[oa]?|grat[oa]|perfeito|show|[oó]timo|maravilha|'
    r'combinado|fechado|isso|mesmo|sim|entendi|top|legal|muito|demais|'
    r'👍|🙏|❤️|💛|🥰|😊|👏|🙌)'
)
_FECHAMENTO_RE = re.compile(
    r'^' + _FECHAMENTO_TOKEN + r'([\s,!.]+' + _FECHAMENTO_TOKEN + r')*'
    r'[\s!.,👍🙏❤️💛🥰😊👏🙌]*$',
    re.IGNORECASE,
)


# Emojis NEGATIVOS nunca são enfeite: "obrigada 😡" não é fechamento
# tranquilo — o bot/vigia devem tratar como mensagem normal.
_EMOJI_NEGATIVO = set('😡😠🤬💢😤👎😾😭😢')


def _sem_emoji_enfeite(t):
    """Remove emojis/símbolos DECORATIVOS do texto ("Obrigada ✨" → "Obrigada").

    A whitelist de emoji no _FECHAMENTO_RE já falhou 2x (criação e o ✨ da
    conv 1697, 18/08/2026: alerta de 'esperando atendente' à 01:25 + msg de
    contenção pro cliente que só tinha agradecido). Emoji desconhecido agora
    é tratado como enfeite GENERICAMENTE; só os negativos (raiva/choro)
    ficam no texto, porque mudam o sentido."""
    out = []
    for ch in t:
        cp = ord(ch)
        if ch in _EMOJI_NEGATIVO:
            out.append(ch)
        elif cp in (0xFE0F, 0x200D) or 0x1F3FB <= cp <= 0x1F3FF:
            continue                    # variation selector / ZWJ / tom de pele
        elif 0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or cp == 0x2B50:
            continue                    # blocos de emoji/símbolos (✨ = 2728)
        else:
            out.append(ch)
    return ' '.join(''.join(out).split())


def _e_fechamento(texto):
    """True pra mensagens curtas de encerramento/agradecimento — cliente não
    aguarda resposta, não deve disparar 'esperando atendente'.

    Duas passadas: o texto cru (cobre mensagem SÓ de emoji da whitelist,
    tipo "🙏") e o texto sem emojis de enfeite (cobre "Obrigada ✨" e
    qualquer emoji futuro fora da whitelist). Mensagem só de emoji
    desconhecido NÃO vira fechamento (o strip esvazia e a 2ª passada exige
    texto)."""
    t = (texto or '').strip()
    if not t or len(t) > 30:
        return False
    if _FECHAMENTO_RE.match(t):
        return True
    limpo = _sem_emoji_enfeite(t)
    return bool(limpo) and limpo != t and bool(_FECHAMENTO_RE.match(limpo))


def _e_mencao_story(texto):
    """True pra marcação em story do Instagram (decisão do dono 06/07/2026:
    'não precisa se preocupar com menções no Instagram'). O Chatwoot entrega
    a menção como mensagem do cliente ('fulana mentioned you in the story:')
    e o alerta de 'cliente esperando atendente' disparava — mas ninguém está
    esperando nada: é marcação social, não atendimento."""
    t = (texto or '').lower()
    return ('mentioned you in the story' in t
            or 'mencionou você no story' in t
            or 'mencionou voce no story' in t)


# Mensagem de CONTENÇÃO pro cliente esperando atendente (dono 09/08/2026).
# Sem prazo prometido de propósito — só confirma que a mensagem foi vista.
TEXTO_CONTENCAO_ESPERA = (
    'Recebemos a sua mensagem! 🙏 Nossa equipe está com o atendimento em '
    'alta demanda neste momento, mas já vai te responder por aqui. '
    'Obrigado pela paciência!')


def alertar_clientes_esperando_humano(min_minutos=10, max_minutos=720,
                                       max_por_ciclo=5):
    """Detector C (12/06/2026, conv #198): cliente manda mensagem em
    conversa `open` (humano e dono da conversa) e NINGUEM responde.

    O bot ignora `open` por design (humano assumiu); o detector de
    abandono so olha `pending`. Resultado: cliente esperando atendente
    era INVISIVEL pra todos os vigias — a Mariana mandou 'Olá' as 17:42
    e ficou no vacuo.

    Deterministico (sem Haiku): conversa open + ultima mensagem e do
    CLIENTE + parada entre min e max minutos = fato, nao julgamento.
    Dedupe persistente em VigiaVeredito ('[ESPERA_HUMANO'), mesmo padrao
    do abandono. Alerta o dono via Z-API."""
    from app.services import chatwoot, zapi

    numero = _numero_destino()
    if not numero:
        return {'pulou': 'sem numero destino'}

    paradas = chatwoot.listar_conversas_paradas(
        min_minutos=min_minutos, status='open')
    avaliadas = enviadas = 0
    for c in paradas:
        if enviadas >= max_por_ciclo:
            break
        conv_id = c.get('id')
        minutos = c.get('minutos_paradas', 0)
        if not conv_id or minutos > max_minutos:
            continue
        if _ja_avisado_espera_humano(conv_id):
            continue
        historico = chatwoot.buscar_historico(conv_id)
        if not historico:
            continue
        # So alerta se quem falou por ultimo foi o CLIENTE (esperando).
        # Ultima do atendente = atendimento em andamento, nao alertar.
        if historico[-1].get('role') != 'user':
            continue
        # "Ok"/"obrigada"/"valeu" = encerramento, cliente nao espera resposta.
        if _e_fechamento(historico[-1].get('content')):
            continue
        # Marcacao em story do IG nao e cliente esperando (dono, 06/07/2026).
        if _e_mencao_story(historico[-1].get('content')):
            continue
        avaliadas += 1
        ultima = (historico[-1].get('content') or '')[:120]
        nome = c.get('nome_contato') or '(sem nome)'
        # Chave do CONTATO (telefone ou identifier do IG, so \w): o dedupe
        # por conversa nao basta — caso Lissa 19/08/2026: a MESMA cliente
        # em DUAS conversas do Chatwoot (1723 e 1730) recebeu a contencao
        # em cada uma, e no Instagram as duas caem na MESMA thread =
        # mensagem duplicada na tela dela.
        import re as _re
        chave_contato = _re.sub(r'\W+', '', (c.get('telefone') or ''))[:40]
        cfg = current_app.config
        base_cw = (cfg.get('CHATWOOT_URL') or '').rstrip('/')
        acc = (cfg.get('CHATWOOT_ACCOUNT_ID') or '').strip()
        link = (f'{base_cw}/app/accounts/{acc}/conversations/{conv_id}'
                if base_cw and acc else '')
        msg = (f'🙋 *Cliente esperando ATENDENTE* ha {minutos}min\n'
               f'Cliente: {nome} (conversa #{conv_id})\n\n'
               f'Última mensagem: "{ultima}"\n\n'
               'O bot nao responde conversas que ja foram assumidas por '
               'humano — alguem da equipe precisa olhar.'
               + (f'\n\n{link}' if link else ''))
        # CLAIM-FIRST (20/08/2026): o registro É o dedupe, então ele tem que
        # estar COMMITADO antes do envio. Na ordem antiga (envia → registra)
        # dois processos rodando o mesmo ciclo com segundos de diferença —
        # os 2 workers gunicorn, ou container velho + novo no meio de um
        # deploy — passavam os dois pelo `_ja_avisado_espera_humano` e o
        # dono recebia o alerta em dobro. Envio falho DESFAZ o claim (o
        # cliente esperando não pode ficar sem aviso por um erro de rede).
        claim = _registrar_espera_humano(conv_id, nome, minutos, ultima,
                                         False, contato_chave=chave_contato)
        if claim is None:
            logger.warning('espera-humano: claim falhou conv=%s — pula '
                           'este ciclo (retenta no proximo)', conv_id)
            continue
        try:
            envio = zapi.enviar_texto(numero, msg)
        except Exception:  # noqa: BLE001
            logger.exception('espera-humano: envio falhou conv=%s', conv_id)
            _desfazer_claim_espera(claim)
            continue
        if not envio.get('ok'):
            # Z-API fora / segurada pelo teto: devolve o claim pra retentar.
            _desfazer_claim_espera(claim)
            continue
        _confirmar_envio_espera(claim)
        # CONTENÇÃO ao CLIENTE (dono 09/08/2026, Dia dos Pais: 12 clientes
        # esperando 10-14min em conversa open enquanto a equipe entregava —
        # inclusive VENDA esperando): junto com o alerta ao dono, o cliente
        # recebe UM aviso de que foi visto ("a equipe já te responde").
        # Não promete prazo nem responde a dúvida — só tira o cliente do
        # vácuo. Dedupe herdado do alerta (1x/12h por conversa, o registro
        # acima). Best-effort: falha nunca derruba o alerta ao dono.
        # Kill-switch: ESPERA_HUMANO_CONTENCAO=0 (env direto — a armadilha
        # do Spotify: env nova só chega ao app.config se declarada no
        # config.py; kill-switch de vigia lê os.environ como os demais).
        import os as _os
        if _os.environ.get('ESPERA_HUMANO_CONTENCAO', '1') != '0':
            # Anti-duplicidade em DUAS camadas (caso Lissa, 19/08/2026):
            # (a) o texto ja esta NESTA conversa (re-alerta pos-12h nao
            # re-manda o mesmo aviso pro cliente); (b) o MESMO CONTATO ja
            # recebeu contencao em OUTRA conversa nas ultimas 12h — no IG
            # varias conversas Chatwoot caem numa unica thread do cliente.
            ja_nesta = any(
                TEXTO_CONTENCAO_ESPERA[:40] in (m.get('content') or '')
                for m in historico[-15:] if m.get('role') != 'user')
            if ja_nesta or _contencao_recente_para_contato(chave_contato,
                                                          conv_id):
                logger.info('espera-humano: contenção suprimida conv=%s '
                            '(duplicaria pro contato)', conv_id)
            else:
                try:
                    chatwoot.enviar_mensagem(conv_id, TEXTO_CONTENCAO_ESPERA)
                except Exception:  # noqa: BLE001
                    logger.exception('espera-humano: contenção falhou '
                                     'conv=%s', conv_id)
        if envio.get('ok'):
            enviadas += 1
            logger.info('espera-humano alertado conv=%s (%smin)',
                        conv_id, minutos)
    return {'avaliadas': avaliadas, 'enviadas': enviadas}


def _ja_avisado_espera_humano(conv_id, horas=12):
    """Dedupe persistente (mesmo padrao do abandono). 12h: se o cliente
    seguir esperando no dia seguinte, vale re-alertar."""
    try:
        from datetime import timedelta

        from app.models import VigiaVeredito
        from app.utils import agora
        corte = agora() - timedelta(hours=horas)
        return (VigiaVeredito.query
                .filter(VigiaVeredito.conv_id == str(conv_id),
                        VigiaVeredito.criado_em >= corte,
                        VigiaVeredito.mensagem_cliente.like('[ESPERA_HUMANO%'))
                .first()) is not None
    except Exception:  # noqa: BLE001
        logger.exception('espera-humano: dedupe falhou (assume avisado)')
        return True   # fail-closed: na duvida nao re-alerta


def _contencao_recente_para_contato(chave, conv_id, horas=12):
    """True se ESTE contato (chave \w de telefone/identifier) ja recebeu a
    contencao nas ultimas N horas em QUALQUER conversa — no Instagram, mais
    de uma conversa Chatwoot desagua na mesma thread do cliente (caso
    Lissa 19/08/2026). Chave vazia = sem como cruzar, nao bloqueia (as
    guardas por conversa seguem valendo). Erro = True (na duvida, nao
    repete texto enlatado pro cliente)."""
    if not chave:
        return False
    try:
        from datetime import timedelta

        from app.models import VigiaVeredito
        from app.utils import agora
        corte = agora() - timedelta(hours=horas)
        # Exclui a PROPRIA conversa: o registro desta rodada e gravado
        # ANTES da contencao — sem o filtro, o guard ve o proprio registro
        # e suprime ate o primeiro envio.
        return (VigiaVeredito.query
                .filter(VigiaVeredito.criado_em >= corte,
                        VigiaVeredito.conv_id != str(conv_id),
                        VigiaVeredito.mensagem_cliente.like(
                            f'[ESPERA_HUMANO%c:{chave}]%'))
                .first()) is not None
    except Exception:  # noqa: BLE001
        logger.exception('espera-humano: dedupe por contato falhou '
                         '(assume enviado)')
        return True


def _registrar_espera_humano(conv_id, nome, minutos, ultima_msg, enviado,
                             contato_chave=''):
    """Grava o veredito de espera-humano e devolve o ID da linha (None se
    falhou). O ID é o CLAIM: quem chamou envia depois e confirma/desfaz —
    ver `alertar_clientes_esperando_humano`."""
    try:
        from app.extensions import db
        from app.models import VigiaVeredito
        row = VigiaVeredito(
            conv_id=str(conv_id),
            cliente=(nome or '')[:200] or None,
            mensagem_cliente=(f'[ESPERA_HUMANO {minutos}min '
                              f'c:{contato_chave}] {ultima_msg}')[:2000],
            bot_acao='espera_humano',
            alerta=True,
            gravidade='alta',
            motivo_vigia='cliente esperando atendente em conversa open',
            enviado_whatsapp=bool(enviado),
        )
        db.session.add(row)
        db.session.commit()
        return row.id
    except Exception:  # noqa: BLE001
        logger.exception('espera-humano: registro falhou')
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def _confirmar_envio_espera(row_id):
    """Marca `enviado_whatsapp=True` no claim depois do envio OK."""
    try:
        from app.extensions import db
        from app.models import VigiaVeredito
        row = db.session.get(VigiaVeredito, row_id)
        if row is not None:
            row.enviado_whatsapp = True
            db.session.commit()
    except Exception:  # noqa: BLE001
        logger.exception('espera-humano: confirmar envio falhou')
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


def _desfazer_claim_espera(row_id):
    """Apaga o claim quando o envio NÃO saiu — sem isso a linha viraria um
    dedupe de 12h para um alerta que o dono nunca recebeu (cliente ficaria
    esperando em silêncio). Best-effort: falha aqui só mantém a supressão
    até o próximo dia."""
    try:
        from app.extensions import db
        from app.models import VigiaVeredito
        row = db.session.get(VigiaVeredito, row_id)
        if row is not None:
            db.session.delete(row)
            db.session.commit()
    except Exception:  # noqa: BLE001
        logger.exception('espera-humano: desfazer claim falhou')
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


# ── Alertas no painel de entregas (banner + som + historico) ───────────
#
# 15/06/2026 (decisao do dono): alem do WhatsApp, os alertas ALTA do vigia
# aparecem num banner pulsante com som "chato" no /entregas/painel. O som
# so para quando alguem CLICA no banner (= reconhece), server-side (silencia
# em todos os aparelhos). Uma aba lateral lista o historico com o link da
# conversa no Chatwoot pra resolver.
#
# Reusa VigiaVeredito (mesma fonte do WhatsApp): alerta=True, gravidade='alta'.
# Pendente = ainda nao reconhecido E dentro da janela. Apos a janela, sai do
# banner (o WhatsApp ja notificou; o banner eh nudge ao vivo) mas continua
# no historico.

# Janela (horas) em que um alerta nao-reconhecido ainda alarma no painel.
# Configuravel: VIGIA_PAINEL_JANELA_HORAS (default 8 — cobre um turno).
def _janela_horas():
    try:
        return int(os.environ.get('VIGIA_PAINEL_JANELA_HORAS', '8'))
    except (TypeError, ValueError):
        return 8


def link_chatwoot(conv_id):
    """URL da conversa no Chatwoot, ou '' se nao der pra montar.

    So monta pra conv_id NUMERICO (conversas reais). conv_id sintetico
    ('teste-estoque', '0', etc) nao vira link — evita mandar a equipe
    pra uma URL quebrada."""
    cid = str(conv_id or '').strip()
    if not cid.isdigit():
        return ''
    cfg = current_app.config
    base_cw = (cfg.get('CHATWOOT_URL') or '').rstrip('/')
    acc = (cfg.get('CHATWOOT_ACCOUNT_ID') or '').strip()
    if not (base_cw and acc):
        return ''
    return f'{base_cw}/app/accounts/{acc}/conversations/{cid}'


def _serializar_alerta(v):
    """Dict compacto de um VigiaVeredito pro front do painel."""
    from app.utils import agora as _ag
    criado = v.criado_em
    # "ha quanto tempo" simples, pro banner ("ha 12min")
    minutos = None
    if criado:
        try:
            minutos = int((_ag() - criado).total_seconds() // 60)
        except Exception:  # noqa: BLE001
            minutos = None
    return {
        'id': v.id,
        'gravidade': v.gravidade,
        'cliente': v.cliente or '(sem nome)',
        'motivo': v.motivo_vigia or v.bot_motivo or '',
        'mensagem_cliente': (v.mensagem_cliente or '')[:300],
        'conv_id': v.conv_id,
        'chatwoot_url': link_chatwoot(v.conv_id),
        'criado_em': criado.isoformat() if criado else None,
        'ha_minutos': minutos,
        'reconhecido': v.reconhecido_em is not None,
    }


def _query_pendentes():
    """VigiaVeredito ALTA, nao reconhecido, dentro da janela — mais novos
    primeiro."""
    from datetime import timedelta

    from app.models import VigiaVeredito
    from app.utils import agora as _ag
    corte = _ag() - timedelta(hours=_janela_horas())
    return (VigiaVeredito.query
            .filter(VigiaVeredito.alerta.is_(True),
                    VigiaVeredito.gravidade == 'alta',
                    VigiaVeredito.reconhecido_em.is_(None),
                    VigiaVeredito.criado_em >= corte)
            .order_by(VigiaVeredito.criado_em.desc()))


def alertas_pendentes_resumo():
    """Pro api_painel: {pendentes: N, ultimo: {..}|None}. Barato — 1 query."""
    pend = _query_pendentes().limit(50).all()
    return {
        'pendentes': len(pend),
        'ultimo': _serializar_alerta(pend[0]) if pend else None,
    }


def reconhecer_pendentes(user_id=None, ids=None):
    """Marca alertas como reconhecidos (clique no banner). Sem `ids`, marca
    TODOS os pendentes da janela. Retorna quantos foram marcados."""
    from app.extensions import db
    from app.utils import agora as _ag
    q = _query_pendentes()
    if ids:
        from app.models import VigiaVeredito
        q = q.filter(VigiaVeredito.id.in_(list(ids)))
    marcados = 0
    momento = _ag()
    for v in q.all():
        v.reconhecido_em = momento
        v.reconhecido_por_id = user_id
        marcados += 1
    if marcados:
        db.session.commit()
        logger.info('vigia painel: %s alerta(s) reconhecido(s) por uid=%s',
                    marcados, user_id)
    return marcados


def historico_alertas(limite=40, janela_horas=48):
    """Pro drawer lateral: ultimos alertas ALTA (reconhecidos ou nao) da
    janela, com link do Chatwoot. Mais recentes primeiro."""
    from datetime import timedelta

    from app.models import VigiaVeredito
    from app.utils import agora as _ag
    corte = _ag() - timedelta(hours=janela_horas)
    rows = (VigiaVeredito.query
            .filter(VigiaVeredito.alerta.is_(True),
                    VigiaVeredito.gravidade == 'alta',
                    VigiaVeredito.criado_em >= corte)
            .order_by(VigiaVeredito.criado_em.desc())
            .limit(limite).all())
    return [_serializar_alerta(v) for v in rows]

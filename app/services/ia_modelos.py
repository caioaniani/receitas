"""Modelo de IA do sistema e os parâmetros que dependem dele — fonte única.

Decisão do dono (28/09/2026): toda IA e todo bot do sistema rodam
**Claude Sonnet 5.5** (`claude-sonnet-5-5`), substituindo o Sonnet 5 que
valia desde 05/08/2026.

Por que um módulo e não só uma constante: o Sonnet 5.5 recusa com 400
`thinking: {"type": "disabled"}` — o jeito de desligar o raciocínio nele é
`{"type": "between_tools"}`, que por sua vez NENHUM outro modelo aceita. As
envs do Railway (OCR_MODELO_OPUS, CADASTRO_IA_MODELO, PLANEJAMENTO_IA_MODELO,
GOOGLE_REVIEWS_IA_MODELO, TREINO_IA_MODELO, ZAPI_BOT_MODELO) sobrescrevem o
modelo por função; se cada chamada escrevesse o parâmetro à mão, uma env com
modelo antigo passaria a receber `between_tools` e morreria em 400 — em
silêncio, porque várias dessas chamadas são best-effort (a mesma classe do
incidente de 05-17/08/2026, quando o SDK velho recusava `thinking` e seis
funções ficaram mortas por duas semanas). Aqui o parâmetro sai do MODELO QUE
VAI RODAR.

Regras (guia oficial de migração, skill claude-api):
- Sonnet 5.5: `between_tools` é o nível mais baixo de raciocínio; só vale com
  effort `high` ou abaixo e sem nenhum outro campo dentro de `thinking`.
- Effort (decisão do dono, 28/09/2026: "modo médio"): `medium` explícito em
  toda família que aceita o parâmetro, com e sem ferramentas — inclusive o
  Sonnet 5 do fallback de recusa e os modelos que uma env põe no lugar. O
  padrão da API seria `high`; no 5.5 os níveis foram recalibrados e o guia
  indica `medium` como ponto de partida para uso de ferramentas.
- Sonnet 5.5, Opus 5.5 e Fable: o texto que o modelo escreve junto de uma
  chamada de ferramenta (mais de uma ou duas frases) volta como bloco
  `thinking` — vazio no padrão. O copilot usa esse texto como explicação do
  preview; por isso as rotas com ferramentas pedem `display: "updates"`
  (beta `thinking-display-updates-2026-08-18`), que devolve SÓ essas notas de
  progresso e continua escondendo o raciocínio.
- Opus 5.5 / Fable: o raciocínio não se desliga (400 em qualquer effort);
  o mais próximo é effort `low`.
- Sonnet 5 / Opus 5 / Opus 4.x / Sonnet 4.6: `disabled` continua aceito.
- Haiku 4.5, Sonnet 4.5 e modelo desconhecido: nenhum parâmetro — omitir é
  aceito por qualquer modelo; é o lado seguro quando não sabemos a família.

O casamento é pelo PREFIXO MAIS LONGO: `claude-sonnet-5-5` começa com
`claude-sonnet-5` (e `claude-opus-5-5` com `claude-opus-5`); casar pelo
primeiro prefixo mandaria `disabled` ao 5.5. `uso_ia` usa o mesmo casamento
para o preço.
"""
import logging
import os

logger = logging.getLogger(__name__)

MODELO_PADRAO = 'claude-sonnet-5-5'

# Nível de effort de toda chamada (decisão do dono, 28/09/2026: "modo médio").
# Vale para toda família com `aceita_effort` — o 5.5, o Sonnet 5 do fallback
# de recusa e os modelos de env. Precisa ficar em `high` ou abaixo: acima
# disso o `between_tools` das rotas sem ferramentas do 5.5 dá 400.
EFFORT_PADRAO = 'medium'

BETA_PROGRESSO = 'thinking-display-updates-2026-08-18'

# Por família:
#   desligar: dict de `thinking` para as rotas sem ferramentas;
#             'nao_desliga' = o modelo não aceita desligar (usa effort low);
#             None = omitir (o modelo já roda sem raciocínio).
#   progresso: o modelo devolve o texto entre ferramentas em blocos thinking
#              e aceita `display: "updates"`.
#   aceita_effort: o modelo aceita `output_config.effort`; recebe
#                  EFFORT_PADRAO (o `low` de quem não desliga o raciocínio,
#                  nas rotas sem ferramentas, vem antes).
_FAMILIAS = {
    'claude-sonnet-5-5': {'desligar': {'type': 'between_tools'},
                          'progresso': True, 'aceita_effort': True},
    'claude-sonnet-5': {'desligar': {'type': 'disabled'},
                        'progresso': False, 'aceita_effort': True},
    'claude-opus-5-5': {'desligar': 'nao_desliga',
                        'progresso': True, 'aceita_effort': True},
    'claude-opus-5': {'desligar': {'type': 'disabled'},
                      'progresso': False, 'aceita_effort': True},
    'claude-fable-5': {'desligar': 'nao_desliga',
                       'progresso': True, 'aceita_effort': True},
    'claude-opus-4-8': {'desligar': {'type': 'disabled'},
                        'progresso': False, 'aceita_effort': True},
    'claude-opus-4-7': {'desligar': {'type': 'disabled'},
                        'progresso': False, 'aceita_effort': True},
    'claude-opus-4-6': {'desligar': {'type': 'disabled'},
                        'progresso': False, 'aceita_effort': True},
    'claude-sonnet-4-6': {'desligar': {'type': 'disabled'},
                          'progresso': False, 'aceita_effort': True},
    # Sonnet 4.5 e Haiku 4.5: effort não é garantido — omitir é o lado
    # seguro (qualquer modelo aceita a chamada sem o parâmetro).
    'claude-sonnet-4-5': {'desligar': None, 'progresso': False,
                          'aceita_effort': False},
    'claude-haiku-4-5': {'desligar': None, 'progresso': False,
                         'aceita_effort': False},
}


class RespostaRecusada(Exception):
    """A IA recusou pelas salvaguardas da Anthropic (`stop_reason='refusal'`,
    HTTP 200). A mensagem é legível em português: vários caminhos de falha
    mostram `str(exc)` na tela."""

    def __init__(self, categoria):
        self.categoria = categoria
        super().__init__(
            'a IA recusou este conteúdo pelas regras de segurança da '
            f'Anthropic (categoria: {categoria}) — revise o conteúdo ou '
            'faça manualmente')


def _normalizar(modelo):
    m = (modelo or '').strip().lower()
    if m.startswith('anthropic.'):  # id de plataforma (Bedrock)
        m = m[len('anthropic.'):]
    return m


def casar_prefixo(modelo, chaves):
    """A chave de `chaves` que casa com `modelo` pelo prefixo MAIS LONGO, com
    fronteira ('-' de sufixo de data/versão ou '@' da Vertex), ou None.
    'claude-sonnet-5-5' casa 'claude-sonnet-5-5', nunca 'claude-sonnet-5'
    quando as duas existem; 'claude-haiku-4-5-20251001' casa
    'claude-haiku-4-5'."""
    m = _normalizar(modelo)
    candidatas = [c for c in chaves
                  if m == c or m.startswith(c + '-') or m.startswith(c + '@')]
    return max(candidatas, key=len) if candidatas else None


def familia(modelo):
    """Chave da família do `modelo` em `_FAMILIAS`, ou None se desconhecido."""
    return casar_prefixo(modelo, _FAMILIAS)


def modelo_configurado(valor, origem='config'):
    """Normaliza um modelo vindo de configuração (env do Railway ou
    `app.config`): sem espaços nas pontas; vazio vale o padrão. Um modelo que
    o sistema não conhece segue valendo (quem define sabe o que pediu), mas
    fica no log: os parâmetros de raciocínio caem no lado seguro (nenhum).
    Fonte única da regra — a chamada e a visão por função (/admin/debug-ia)
    usam esta mesma função, para nunca divergirem."""
    limpo = (valor or '').strip() if isinstance(valor, str) else ''
    modelo = limpo or MODELO_PADRAO
    if limpo and familia(modelo) is None:
        logger.warning('ia_modelos: %s=%r não é um modelo conhecido — sem '
                       'parâmetros de raciocínio', origem, limpo)
    return modelo


def modelo_do_ambiente(env_var=None):
    """Modelo de uma função: a env (quando definida e não vazia) manda sobre o
    padrão — é a válvula do dono no Railway. Env vazia vale o padrão (antes,
    `os.environ.get(VAR, padrão)` devolvia '' e a chamada ia sem modelo)."""
    valor = os.environ.get(env_var) if env_var else None
    return modelo_configurado(valor, env_var or 'env')


def opcoes_sem_raciocinio(modelo):
    """kwargs de `messages.create` para rotas SEM ferramentas (classificadores,
    extratores, geradores de JSON e de texto curto): o mínimo de raciocínio que
    o `modelo` aceita, para o raciocínio não comer o `max_tokens`."""
    fam = _FAMILIAS.get(familia(modelo) or '')
    if not fam:
        return {}
    if fam['desligar'] == 'nao_desliga':
        return {'output_config': {'effort': 'low'}}
    opcoes = {}
    if fam['desligar']:
        opcoes['thinking'] = dict(fam['desligar'])
    if fam['aceita_effort']:
        # between_tools só é aceito com effort high ou abaixo.
        opcoes['output_config'] = {'effort': EFFORT_PADRAO}
    return opcoes


def opcoes_com_ferramentas(modelo):
    """kwargs de `messages.create` para rotas COM ferramentas (copilot e o bot
    de atendimento): raciocínio adaptativo (o padrão dos modelos atuais — ajuda
    a escolher e usar as ferramentas) e, onde o texto entre ferramentas vira
    bloco thinking, `display: "updates"` para esse texto voltar legível."""
    fam = _FAMILIAS.get(familia(modelo) or '')
    if not fam:
        return {}
    opcoes = {}
    if fam['progresso']:
        opcoes['thinking'] = {'type': 'adaptive', 'display': 'updates'}
        opcoes['extra_headers'] = {'anthropic-beta': BETA_PROGRESSO}
    if fam['aceita_effort']:
        opcoes['output_config'] = {'effort': EFFORT_PADRAO}
    return opcoes


def mostra_progresso(opcoes):
    """True quando as `opcoes` pediram `display: "updates"` — só então um
    bloco thinking com texto é nota de progresso para o usuário (com
    `summarized`, seria resumo do raciocínio, que não se mostra)."""
    return ((opcoes or {}).get('thinking') or {}).get('display') == 'updates'


def modelo_servido(resp, pedido):
    """Modelo que de fato respondeu — o do fallback, quando `criar` refez a
    chamada após uma recusa; `pedido` quando a resposta não diz."""
    servido = getattr(resp, 'model', None)
    return servido if isinstance(servido, str) and servido else pedido


def progresso_na_resposta(resp, modelo_pedido):
    """`mostra_progresso` para a resposta que VOLTOU numa rota com
    ferramentas: quando `criar` refez a chamada noutro modelo (fallback de
    recusa), vale o que foi pedido a ESSE modelo, não ao original."""
    return mostra_progresso(
        opcoes_com_ferramentas(modelo_servido(resp, modelo_pedido)))


def textos_visiveis(resp, progresso=False):
    """Textos que o usuário pode ler, na ordem: blocos `text` e, com
    `progresso`, as notas de progresso (blocos thinking não vazios)."""
    partes = []
    for bloco in getattr(resp, 'content', None) or []:
        tipo = getattr(bloco, 'type', None)
        if tipo == 'text':
            texto = getattr(bloco, 'text', '') or ''
        elif tipo == 'thinking' and progresso:
            texto = getattr(bloco, 'thinking', '') or ''
        else:
            continue
        if texto.strip():
            partes.append(texto.strip())
    return partes


def _categoria_recusa(resp):
    if getattr(resp, 'stop_reason', None) != 'refusal':
        return None
    detalhes = getattr(resp, 'stop_details', None)
    return getattr(detalhes, 'category', None) or 'sem_categoria'


def recusa(resp):
    """Categoria da recusa quando a resposta foi recusada pelas salvaguardas
    (`stop_reason == 'refusal'`, HTTP 200), senão None.

    Recusa não é resposta: o `content` vem vazio ou PARCIAL e não pode ser
    lido como completo. O Sonnet 5.5 recusa em cinco categorias (cyber, bio,
    frontier_llm, reasoning_extraction, general_harms — esta pode pegar
    trabalho benigno). Categoria ausente vira 'sem_categoria'."""
    categoria = _categoria_recusa(resp)
    if categoria:
        logger.warning('IA recusou a resposta (modelo=%s, categoria=%s)',
                       getattr(resp, 'model', '?'), categoria)
    return categoria


def exigir_resposta(resp):
    """Levanta `RespostaRecusada` quando a resposta é uma recusa — para o
    chamador tratar pelo caminho de falha que já tem, antes de ler o texto."""
    categoria = recusa(resp)
    if categoria:
        raise RespostaRecusada(categoria)
    return resp


# Falhas que se resolvem tentando de novo: tempo esgotado (408), conflito
# (409), limite de taxa (429), erro do servidor e sobrecarga (5xx, 529) e
# entrada grande demais (413, depende da entrada, não do sistema). Todo o
# resto se repete em TODA execução: parâmetro recusado (400), chave inválida
# (401), sem permissão (403), modelo inexistente (404) — e exceção que nem
# chegou à rede (TypeError do SDK que não conhece um parâmetro, a causa do
# incidente de 05-17/08/2026, ou erro de código nosso).
_STATUS_TRANSITORIO = frozenset({408, 409, 413, 429})


def falha_permanente(exc):
    """True quando a falha se repete em TODA execução — a função está morta
    até alguém mexer (a classe do incidente de 05-17/08/2026)."""
    try:
        import anthropic
        if isinstance(exc, anthropic.APIConnectionError):  # inclui timeout
            return False
    except ImportError:  # pragma: no cover — SDK é dependência do app
        pass
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return False
    status = getattr(exc, 'status_code', None)
    if isinstance(status, int):
        return not (status in _STATUS_TRANSITORIO or status >= 500)
    return True


def registrar_falha(log, contexto, exc):
    """Log de uma chamada de IA que falhou, no nível certo: erro permanente
    vira ERROR (vai ao Sentry — uma função morta por parâmetro recusado não
    pode passar semanas em WARNING, como em 05-17/08/2026); falha transitória
    (rede, timeout, 429, 5xx, sobrecarga) segue WARNING, pela política de
    ruído do Sentry (o próximo uso tenta de novo)."""
    if falha_permanente(exc):
        log.error('%s: a chamada de IA falhou de forma permanente (%s, HTTP '
                  '%s — parâmetro, chave, modelo ou código): %s', contexto,
                  type(exc).__name__, getattr(exc, 'status_code', '-'), exc,
                  exc_info=exc)
    else:
        log.warning('%s: falha na chamada: %s', contexto, exc)


# ── A chamada ──────────────────────────────────────────────────────────

# Fallback de RECUSA (item [BLOCKS] do guia do Sonnet 5.5). O fallback do
# servidor da Anthropic (`fallbacks: "default"`, beta
# server-side-fallback-2026-07-01) refaz no Sonnet 5 só as recusas `cyber` e
# `frontier_llm` do Sonnet 5.5. O sistema faz o MESMO no cliente, em vez de
# mandar um beta em TODA chamada: uma mudança no beta derrubaria todas as
# funções de IA de uma vez (a classe do incidente de 05-17/08/2026), e o
# retry no cliente só custa algo quando a recusa acontece. As outras
# categorias (bio, reasoning_extraction, general_harms) não têm fallback —
# igual ao servidor. O modelo do fallback recebe os parâmetros DELE (no
# Sonnet 5, `disabled`; `between_tools` daria 400) e não lê os blocos thinking
# do 5.5, que a API descarta sem erro. Desliga com IA_FALLBACK_RECUSA=0.
_FALLBACK_RECUSA = {
    'claude-sonnet-5-5': ('claude-sonnet-5',
                          frozenset({'cyber', 'frontier_llm'})),
}


def modelo_de_fallback(modelo, categoria):
    """Modelo em que uma recusa de `categoria` no `modelo` é refeita, ou
    None."""
    regra = _FALLBACK_RECUSA.get(familia(modelo) or '')
    if not regra or not categoria:
        return None
    destino, categorias = regra
    return destino if categoria in categorias else None


def _fallback_ligado():
    return (os.environ.get('IA_FALLBACK_RECUSA') or '1').strip() != '0'


def _opcoes(modelo, ferramentas):
    return (opcoes_com_ferramentas(modelo) if ferramentas
            else opcoes_sem_raciocinio(modelo))


def criar(client, funcao, *, model, ferramentas=False, canal=None,
          **kwargs):
    """A chamada à API de mensagens do sistema — TODA função de IA passa por
    aqui (há trava por AST nos testes):
    - os parâmetros de raciocínio saem do `model` que vai rodar
      (`ferramentas=True` nas rotas com tools);
    - o custo de CADA tentativa vai para UsoIA com o modelo que a atendeu
      (sessão isolada, best-effort);
    - recusa `cyber`/`frontier_llm` do Sonnet 5.5 é refeita UMA vez no
      Sonnet 5 (ver _FALLBACK_RECUSA).

    Devolve a resposta final. O chamador continua obrigado a conferir
    `recusa()`/`exigir_resposta()` antes de ler o conteúdo: a outra categoria,
    ou o fallback, também podem recusar. Exceção da API sobe como antes."""
    from app.services import uso_ia

    resp = client.messages.create(model=model, **_opcoes(model, ferramentas),
                                  **kwargs)
    uso_ia.registrar(funcao, model, getattr(resp, 'usage', None),
                     canal=canal)
    categoria = _categoria_recusa(resp)
    destino = modelo_de_fallback(model, categoria)
    if destino and _fallback_ligado():
        logger.warning('%s: recusa (%s) no %s — refazendo no %s', funcao,
                       categoria, model, destino)
        resp = client.messages.create(model=destino,
                                      **_opcoes(destino, ferramentas),
                                      **kwargs)
        uso_ia.registrar(funcao, destino, getattr(resp, 'usage', None),
                         canal=canal)
    return resp


# ── Visibilidade e verificação ─────────────────────────────────────────


# Funções cujas chamadas levam ferramentas (`criar(..., ferramentas=True)`):
# o bot de atendimento e o copilot. Só servem para a visão por função mostrar
# o effort da rota certa; um teste por AST trava que nenhum outro arquivo
# passe a chamar com ferramentas sem entrar aqui.
FUNCOES_COM_FERRAMENTAS = frozenset({'bot_atendimento', 'copilot_slack',
                                     'copilot_whatsapp'})


def effort_da_chamada(modelo, ferramentas):
    """O `output_config.effort` que a chamada do `modelo` leva nesta rota, ou
    None quando o parâmetro é omitido (vale o padrão do modelo). Sai das
    MESMAS opções que `criar` envia — a visão nunca diverge da chamada."""
    return (_opcoes(modelo, ferramentas).get('output_config') or {}).get(
        'effort')


def modelos_por_funcao():
    """Modelo EFETIVO de cada função no processo que está rodando (envs do
    Railway incluídas) e o effort que cada uma envia. Read-only e sem chamada
    à API: é o que diz, de fora, se alguma função ficou fora do padrão por
    uma env antiga."""
    from flask import current_app, has_app_context

    from app.services import (
        cadastro_ia,
        chatbot,
        chatbot_auditor,
        chatbot_vigia,
        conta_pagar_ia,
        copilot,
        planejamento_ia,
        seo_descricoes,
        treino_ia_perguntas,
    )
    whatsapp = modelo_configurado(
        current_app.config.get('ZAPI_BOT_MODELO') if has_app_context()
        else None, 'ZAPI_BOT_MODELO')
    modelos = {
        'bot_atendimento': chatbot.MODELO,
        'followup': chatbot.FOLLOWUP_MODELO,
        'vigia': chatbot_vigia.MODELO,
        'auditor': chatbot_auditor.MODELO,
        'copilot_slack': copilot.MODELO_DEFAULT,
        'copilot_whatsapp': whatsapp,
        'ocr_cupom': MODELO_PADRAO,
        'ocr_nf': conta_pagar_ia.MODELO,
        'cadastro_ia': cadastro_ia.MODELO,
        'planejamento': planejamento_ia.MODELO,
        'treino_ia_perguntas': treino_ia_perguntas.MODELO,
        'seo': seo_descricoes.MODELO,
        'avaliacao_google': modelo_do_ambiente('GOOGLE_REVIEWS_IA_MODELO'),
    }
    return {
        'padrao': MODELO_PADRAO,
        'por_funcao': modelos,
        'fora_do_padrao': sorted(f for f, m in modelos.items()
                                 if m != MODELO_PADRAO),
        'effort': EFFORT_PADRAO,
        # None = o modelo daquela função não recebe o parâmetro (Sonnet 4.5,
        # Haiku, desconhecido); 'low' = rota sem ferramentas de um modelo que
        # não desliga o raciocínio (Opus 5.5, Fable).
        'effort_por_funcao': {
            f: effort_da_chamada(m, f in FUNCOES_COM_FERRAMENTAS)
            for f, m in modelos.items()},
        # Funções migradas que hoje não chamam a API em produção (decisão de
        # 24/09/2026, atendimento restrito): o modelo vale se forem religadas.
        'desligadas_nos_canais': {
            'bot_atendimento': 'motor de IA do atendimento só roda em teste '
                               'offline desde 24/09/2026',
            'followup': 'retomada automática desativada em 24/09/2026',
        },
    }


def _mesmo_modelo(servido, pedido):
    """O modelo que respondeu é o pedido? Pela família (prefixo mais longo) —
    `claude-sonnet-5-5` começa com `claude-sonnet-5`, então comparar por
    `startswith` daria falso positivo."""
    if familia(pedido):
        return familia(servido) == familia(pedido)
    return (servido or '').startswith(pedido or '')


def verificar_ao_vivo(modelo=None):
    """Duas chamadas MÍNIMAS e reais à API com os mesmos parâmetros que o
    sistema usa — a suíte de testes simula a Anthropic e não prova que a API
    aceita um parâmetro (lição do incidente de 05-17/08/2026). Uma sem
    ferramentas (`opcoes_sem_raciocinio`) e uma com ferramenta
    (`opcoes_com_ferramentas`). Custo de centavos, registrado em UsoIA como
    'verificacao_ia'. Nunca levanta."""
    modelo = modelo or MODELO_PADRAO
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return {'ok': False, 'modelo': modelo,
                'erro': 'ANTHROPIC_API_KEY nao configurada'}
    try:
        import anthropic

        from app.services import uso_ia
    except ImportError as exc:
        return {'ok': False, 'modelo': modelo, 'erro': str(exc)}
    client = anthropic.Anthropic(api_key=api_key, timeout=60, max_retries=1)

    def _rodar(nome, opcoes, **kwargs):
        resultado = {'nome': nome, 'parametros': {
            k: v for k, v in opcoes.items() if k != 'extra_headers'}}
        if 'extra_headers' in opcoes:
            resultado['parametros']['beta'] = opcoes['extra_headers'].get(
                'anthropic-beta')
        try:
            resp = client.messages.create(model=modelo, **opcoes, **kwargs)
        except Exception as exc:  # noqa: BLE001 — o motivo vai na tela
            logger.exception('verificar_ao_vivo: %s falhou', nome)
            resultado.update(ok=False,
                             erro=f'{type(exc).__name__}: {exc}'[:600])
            return resultado
        uso_ia.registrar('verificacao_ia', modelo,
                         getattr(resp, 'usage', None))
        servido = getattr(resp, 'model', '') or ''
        categoria = recusa(resp)
        blocos = [getattr(b, 'type', '?') for b in resp.content or []]
        resultado.update(
            modelo_servido=servido,
            stop_reason=getattr(resp, 'stop_reason', None),
            recusa=categoria,
            blocos=blocos,
            textos=textos_visiveis(resp, progresso=mostra_progresso(opcoes)),
            ok=(not categoria and _mesmo_modelo(servido, modelo)),
        )
        return resultado

    testes = [
        _rodar('sem_ferramentas', opcoes_sem_raciocinio(modelo),
               max_tokens=64,
               messages=[{'role': 'user',
                          'content': 'Teste de integração. Responda apenas: ok'}]),
        _rodar('com_ferramentas', opcoes_com_ferramentas(modelo),
               max_tokens=2000,
               tools=[{
                   'name': 'anotar',
                   'description': 'Anota um texto curto de teste.',
                   'input_schema': {
                       'type': 'object',
                       'properties': {'texto': {'type': 'string'}},
                       'required': ['texto'],
                   },
               }],
               messages=[{'role': 'user', 'content': (
                   'Teste de integração: diga numa frase o que vai fazer e '
                   'chame a ferramenta anotar com o texto "ok".')}]),
    ]
    return {'ok': all(t['ok'] for t in testes), 'modelo': modelo,
            'testes': testes}


def verificar_modelos_em_uso():
    """`verificar_ao_vivo` para CADA modelo distinto que as funções usam de
    fato (o padrão e os que alguma env do Railway pôs no lugar) — uma env
    antiga recebe parâmetros de OUTRA família, e só uma chamada real prova
    que a API os aceita. Cobertura: os modelos de `modelos_por_funcao`, com
    os dois formatos de chamada (sem e com ferramentas)."""
    modelos = sorted(set(modelos_por_funcao()['por_funcao'].values()))
    por_modelo = {m: verificar_ao_vivo(m) for m in modelos}
    return {'ok': all(r.get('ok') for r in por_modelo.values()),
            'modelos': modelos, 'por_modelo': por_modelo}

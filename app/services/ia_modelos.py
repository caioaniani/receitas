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
  Effort continua `high` por padrão, mas os níveis foram recalibrados — fica
  explícito aqui para a escolha não mudar sozinha.
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

# Nível de effort do Sonnet 5.5. `high` é o padrão da API e o que o sistema já
# rodava no Sonnet 5 (nenhuma chamada fixava effort). Sem avaliação medida no
# tráfego real, a escolha segura é não mudar o nível nominal.
EFFORT_PADRAO = 'high'

BETA_PROGRESSO = 'thinking-display-updates-2026-08-18'

# Por família:
#   desligar: dict de `thinking` para as rotas sem ferramentas;
#             'nao_desliga' = o modelo não aceita desligar (usa effort low);
#             None = omitir (o modelo já roda sem raciocínio).
#   progresso: o modelo devolve o texto entre ferramentas em blocos thinking
#              e aceita `display: "updates"`.
#   effort: `output_config.effort` explícito nas rotas deste modelo.
_FAMILIAS = {
    'claude-sonnet-5-5': {'desligar': {'type': 'between_tools'},
                          'progresso': True, 'effort': EFFORT_PADRAO},
    'claude-sonnet-5': {'desligar': {'type': 'disabled'},
                        'progresso': False, 'effort': None},
    'claude-opus-5-5': {'desligar': 'nao_desliga',
                        'progresso': True, 'effort': None},
    'claude-opus-5': {'desligar': {'type': 'disabled'},
                      'progresso': False, 'effort': None},
    'claude-fable-5': {'desligar': 'nao_desliga',
                       'progresso': True, 'effort': None},
    'claude-opus-4-8': {'desligar': {'type': 'disabled'},
                        'progresso': False, 'effort': None},
    'claude-opus-4-7': {'desligar': {'type': 'disabled'},
                        'progresso': False, 'effort': None},
    'claude-opus-4-6': {'desligar': {'type': 'disabled'},
                        'progresso': False, 'effort': None},
    'claude-sonnet-4-6': {'desligar': {'type': 'disabled'},
                          'progresso': False, 'effort': None},
    'claude-sonnet-4-5': {'desligar': None, 'progresso': False,
                          'effort': None},
    'claude-haiku-4-5': {'desligar': None, 'progresso': False,
                         'effort': None},
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


def modelo_do_ambiente(env_var=None):
    """Modelo de uma função: a env (quando definida e não vazia) manda sobre o
    padrão — é a válvula do dono no Railway. Env vazia vale o padrão (antes,
    `os.environ.get(VAR, padrão)` devolvia '' e a chamada ia sem modelo). Uma
    env com modelo que o sistema não conhece segue valendo (quem define sabe o
    que pediu), mas fica no log: os parâmetros de raciocínio caem no lado
    seguro (nenhum)."""
    valor = (os.environ.get(env_var) or '').strip() if env_var else ''
    modelo = valor or MODELO_PADRAO
    if valor and familia(modelo) is None:
        logger.warning('ia_modelos: %s=%r não é um modelo conhecido — sem '
                       'parâmetros de raciocínio', env_var, valor)
    return modelo


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
    if fam['effort']:
        # between_tools só é aceito com effort high ou abaixo.
        opcoes['output_config'] = {'effort': fam['effort']}
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
    if fam['effort']:
        opcoes['output_config'] = {'effort': fam['effort']}
    return opcoes


def mostra_progresso(opcoes):
    """True quando as `opcoes` pediram `display: "updates"` — só então um
    bloco thinking com texto é nota de progresso para o usuário (com
    `summarized`, seria resumo do raciocínio, que não se mostra)."""
    return ((opcoes or {}).get('thinking') or {}).get('display') == 'updates'


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


def recusa(resp):
    """Categoria da recusa quando a resposta foi recusada pelas salvaguardas
    (`stop_reason == 'refusal'`, HTTP 200), senão None.

    Recusa não é resposta: o `content` vem vazio ou PARCIAL e não pode ser
    lido como completo. O Sonnet 5.5 recusa em cinco categorias (cyber, bio,
    frontier_llm, reasoning_extraction, general_harms — esta pode pegar
    trabalho benigno). Categoria ausente vira 'sem_categoria'."""
    if getattr(resp, 'stop_reason', None) != 'refusal':
        return None
    detalhes = getattr(resp, 'stop_details', None)
    categoria = getattr(detalhes, 'category', None) or 'sem_categoria'
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

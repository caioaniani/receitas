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
silêncio, porque essas chamadas são best-effort (a mesma classe do incidente
de 05-17/08/2026, quando o SDK velho recusava `thinking` e seis funções ficaram
mortas por duas semanas). Aqui o parâmetro sai do MODELO QUE VAI RODAR.

Regras (guia oficial de migração, skill claude-api):
- Sonnet 5.5: `between_tools` é o nível mais baixo de raciocínio; só vale com
  effort `high` ou abaixo e sem nenhum outro campo dentro de `thinking`.
  Effort continua `high` por padrão, mas os níveis foram recalibrados — fica
  explícito aqui para a escolha não mudar sozinha.
- Opus 5.5 / Fable: o raciocínio não se desliga (400 em qualquer effort);
  o mais próximo é effort `low`.
- Sonnet 5 / Opus 5 / Opus 4.x / Sonnet 4.6: `disabled` continua aceito.
- Haiku 4.5, Sonnet 4.5 e modelo desconhecido: nenhum parâmetro — omitir é
  aceito por qualquer modelo; é o lado seguro quando não sabemos a família.

O casamento é pelo PREFIXO MAIS LONGO: `claude-sonnet-5-5` começa com
`claude-sonnet-5` (e `claude-opus-5-5` com `claude-opus-5`); casar pelo
primeiro prefixo mandaria `disabled` ao 5.5.
"""
import logging
import os

logger = logging.getLogger(__name__)

MODELO_PADRAO = 'claude-sonnet-5-5'

# Nível de effort das rotas que usam o Sonnet 5.5. `high` é o padrão da API e
# o que o sistema já rodava no Sonnet 5 (nenhuma chamada fixava effort).
EFFORT_PADRAO = 'high'

# Por família: como desligar o raciocínio, como pedir o adaptativo quando o
# modelo não o liga sozinho, e se aceita `output_config.effort`.
#   desligar: dict de `thinking` | 'nao_desliga' (usar effort low) | None
#             (omitir: o modelo já roda sem raciocínio)
#   adaptativo: dict de `thinking` a mandar nas rotas com ferramentas, ou None
#               (omitir: o modelo já roda adaptativo)
_FAMILIAS = {
    'claude-sonnet-5-5': {'desligar': {'type': 'between_tools'},
                          'adaptativo': None, 'effort': True},
    'claude-sonnet-5': {'desligar': {'type': 'disabled'},
                        'adaptativo': None, 'effort': True},
    'claude-opus-5-5': {'desligar': 'nao_desliga',
                        'adaptativo': None, 'effort': True},
    'claude-opus-5': {'desligar': {'type': 'disabled'},
                      'adaptativo': None, 'effort': True},
    'claude-fable-5': {'desligar': 'nao_desliga',
                       'adaptativo': None, 'effort': True},
    'claude-mythos-5': {'desligar': 'nao_desliga',
                        'adaptativo': None, 'effort': True},
    'claude-opus-4-8': {'desligar': {'type': 'disabled'},
                        'adaptativo': {'type': 'adaptive'}, 'effort': True},
    'claude-opus-4-7': {'desligar': {'type': 'disabled'},
                        'adaptativo': {'type': 'adaptive'}, 'effort': True},
    'claude-opus-4-6': {'desligar': {'type': 'disabled'},
                        'adaptativo': {'type': 'adaptive'}, 'effort': True},
    'claude-sonnet-4-6': {'desligar': {'type': 'disabled'},
                          'adaptativo': {'type': 'adaptive'}, 'effort': True},
    'claude-sonnet-4-5': {'desligar': None, 'adaptativo': None,
                          'effort': False},
    'claude-haiku-4-5': {'desligar': None, 'adaptativo': None,
                         'effort': False},
}


def _normalizar(modelo):
    m = (modelo or '').strip().lower()
    if m.startswith('anthropic.'):  # id de plataforma (Bedrock)
        m = m[len('anthropic.'):]
    return m


def familia(modelo):
    """Chave da família do `modelo` em `_FAMILIAS` (prefixo mais longo), ou
    None quando o modelo não é conhecido."""
    m = _normalizar(modelo)
    candidatas = [f for f in _FAMILIAS if m == f or m.startswith(f + '-')
                  or m.startswith(f + '@')]
    return max(candidatas, key=len) if candidatas else None


def modelo_do_ambiente(env_var=None):
    """Modelo de uma função: a env (quando definida e não vazia) manda sobre o
    padrão — é a válvula do dono no Railway. Uma env com modelo que o sistema
    não conhece segue valendo (quem define sabe o que pediu), mas fica no log:
    os parâmetros de raciocínio caem no lado seguro (nenhum)."""
    valor = (os.environ.get(env_var) or '').strip() if env_var else ''
    modelo = valor or MODELO_PADRAO
    if valor and familia(modelo) is None:
        logger.warning('ia_modelos: %s=%r não é um modelo conhecido — sem '
                       'parâmetros de raciocínio', env_var, valor)
    return modelo


def opcoes_sem_raciocinio(modelo):
    """kwargs de `messages.create` para rotas SEM ferramentas (classificadores,
    extratores, geradores de JSON com teto curto): o mínimo de raciocínio que
    o `modelo` aceita, para o raciocínio não comer o `max_tokens`."""
    fam = _FAMILIAS.get(familia(modelo) or '')
    if not fam:
        return {}
    desligar = fam['desligar']
    if desligar == 'nao_desliga':
        return {'output_config': {'effort': 'low'}}
    opcoes = {}
    if desligar:
        opcoes['thinking'] = dict(desligar)
    if familia(modelo) == 'claude-sonnet-5-5':
        # between_tools só é aceito com effort high ou abaixo.
        opcoes['output_config'] = {'effort': EFFORT_PADRAO}
    return opcoes


def opcoes_com_ferramentas(modelo):
    """kwargs de `messages.create` para rotas COM ferramentas (bot de
    atendimento, copilot): raciocínio adaptativo — ajuda a escolher e usar as
    ferramentas. Omitido onde o modelo já roda adaptativo."""
    nome = familia(modelo)
    fam = _FAMILIAS.get(nome or '')
    if not fam:
        return {}
    opcoes = {}
    if fam['adaptativo']:
        opcoes['thinking'] = dict(fam['adaptativo'])
    if nome == 'claude-sonnet-5-5':
        opcoes['output_config'] = {'effort': EFFORT_PADRAO}
    return opcoes


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

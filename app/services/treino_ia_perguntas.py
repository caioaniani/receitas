"""v2 §16.2 — geração de perguntas de quiz por IA, com REVISÃO HUMANA
OBRIGATÓRIA.

A IA só PROPÕE perguntas a partir de um texto (roteiro/resumo/transcrição do
vídeo que o admin cola). Nada é salvo aqui: a rota devolve as propostas, o admin
edita/seleciona na tela e só então as escolhidas viram questões (pelo endpoint
normal de questão). Custo registrado em UsoIA (funcao='treino_ia_perguntas').
Padrão da casa (mesmo de cadastro_ia): Sonnet, mockável nos testes.
"""
import json
import logging
import os
import re

MODELO = os.environ.get('TREINO_IA_MODELO', 'claude-sonnet-5')
logger = logging.getLogger(__name__)

SYSTEM = (
    'Você cria perguntas de MÚLTIPLA ESCOLHA para treinamento de funcionários '
    'de padaria/alimentação, em português correto do Brasil. A partir do '
    'CONTEÚDO fornecido, gere perguntas objetivas, claras e sem pegadinha, cada '
    'uma com 4 alternativas plausíveis e UMA correta. Responda SÓ com JSON: '
    'uma lista de objetos {"enunciado": str, "alternativas": [str, str, str, '
    'str], "correta": int (índice 0-3 da certa), "dificuldade": '
    '"FACIL"|"MEDIA"|"DIFICIL"}. Sem texto fora do JSON.')

SYSTEM_MOMENTO = (
    'Você cria perguntas de MÚLTIPLA ESCOLHA de CHECKPOINT para treinamento de '
    'funcionários de padaria/alimentação, em português correto do Brasil, a '
    'partir da TRANSCRIÇÃO COM TEMPO de um vídeo. Cada linha vem como "[segundo] '
    'texto falado". Para cada pergunta, escolha o MOMENTO (em segundos, tirado '
    'dos tempos da transcrição) LOGO APÓS o assunto ser explicado, pra pausar o '
    'vídeo e cobrar atenção. Responda SÓ com JSON: lista de {"enunciado": str, '
    '"alternativas": [str,str,str,str], "correta": int (0-3), "momento_seg": '
    'int (segundos), "dificuldade": "FACIL"|"MEDIA"|"DIFICIL"}. Sem texto fora '
    'do JSON.')


class _IAError(Exception):
    """Falha amigável da IA (sem chave, resposta inválida, rede)."""


def _chamar(system, instrucao):
    """Chama o modelo e devolve os dados JSON (lista/dict). Levanta _IAError
    com mensagem amigável em qualquer falha. Custo registrado em UsoIA."""
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        raise _IAError('ANTHROPIC_API_KEY não configurada.')
    try:
        import anthropic
    except ImportError as e:
        raise _IAError('biblioteca anthropic não instalada.') from e
    client = anthropic.Anthropic(api_key=api_key, timeout=120, max_retries=1)
    try:
        resp = client.messages.create(
            model=MODELO, max_tokens=3000, system=system,
            messages=[{'role': 'user', 'content': instrucao}])
        from app.services import uso_ia
        uso_ia.registrar('treino_ia_perguntas', MODELO,
                         getattr(resp, 'usage', None))
        bruto = ''.join(b.text for b in resp.content
                        if getattr(b, 'type', '') == 'text')
        bruto = re.sub(r'^```(?:json)?\s*|\s*```$', '', bruto.strip(),
                       flags=re.MULTILINE)
        return json.loads(bruto)
    except json.JSONDecodeError as e:
        raise _IAError('a IA devolveu resposta inválida — tente de novo.') from e
    except Exception as exc:  # noqa: BLE001 — falha de rede/modelo é reportada
        logger.warning('treino_ia_perguntas: falha: %s', exc)
        raise _IAError(f'falha na IA: {exc}') from exc


def gerar(texto, n=5):
    """Propõe até `n` perguntas a partir de `texto` (roteiro/resumo colado).
    Retorna {'perguntas': [...], 'modelo_usado': ...} ou {'erro': ...}. NÃO
    salva nada (revisão humana acontece na tela antes de gravar)."""
    texto = (texto or '').strip()
    if len(texto) < 40:
        return {'erro': 'Cole um conteúdo maior (roteiro/resumo do vídeo) '
                        'pra IA ter do que tirar as perguntas.'}
    n = max(1, min(int(n or 5), 15))
    instrucao = (f'Gere {n} pergunta(s) a partir deste conteúdo:\n\n'
                 f'{texto[:8000]}')
    try:
        dados = _chamar(SYSTEM, instrucao)
    except _IAError as e:
        return {'erro': str(e)}
    perguntas = _sanitizar(dados)
    if not perguntas:
        return {'erro': 'a IA não retornou perguntas utilizáveis.'}
    return {'perguntas': perguntas, 'modelo_usado': MODELO}


def gerar_com_momento(segmentos, n=3):
    """Propõe perguntas de CHECKPOINT a partir da transcrição COM TEMPO do
    vídeo (lista de {'inicio': seg, 'texto': str}). Cada pergunta vem com
    `momento_seg` sugerido. Retorna {'perguntas': [...]} ou {'erro': ...}."""
    segmentos = segmentos or []
    if not segmentos:
        return {'erro': 'Sem transcrição do vídeo ainda.'}
    momento_max = max((int(s.get('inicio') or 0) for s in segmentos), default=0)
    linhas = '\n'.join(f'[{int(s.get("inicio") or 0)}] {s.get("texto") or ""}'
                       for s in segmentos)[:8000]
    n = max(1, min(int(n or 3), 15))
    instrucao = (f'Gere {n} pergunta(s) de checkpoint a partir desta '
                 f'transcrição com tempo (segundos):\n\n{linhas}')
    try:
        dados = _chamar(SYSTEM_MOMENTO, instrucao)
    except _IAError as e:
        return {'erro': str(e)}
    perguntas = _sanitizar(dados, com_momento=True, momento_max=momento_max)
    if not perguntas:
        return {'erro': 'a IA não retornou perguntas utilizáveis.'}
    return {'perguntas': perguntas, 'modelo_usado': MODELO}


def _sanitizar(dados, com_momento=False, momento_max=None):
    """Blinda a proposta da IA: enunciado não-vazio, 2-5 alternativas, índice
    correto válido. Com `com_momento`, `momento_seg` vira int em [0, momento_max]
    (a IA não pode sugerir um tempo além do fim do vídeo) ou `None` quando
    ausente/inválido — o front NÃO exibe "0:00" como se fosse sugestão. Descarta
    o que não bate."""
    if isinstance(dados, dict):
        dados = dados.get('perguntas') or dados.get('questoes') or []
    out = []
    for q in dados or []:
        if not isinstance(q, dict):
            continue
        enun = (q.get('enunciado') or '').strip()
        alts = [str(a).strip() for a in (q.get('alternativas') or [])
                if str(a).strip()]
        try:
            correta = int(q.get('correta'))
        except (TypeError, ValueError):
            correta = -1
        dif = (q.get('dificuldade') or 'MEDIA').upper()
        if dif not in ('FACIL', 'MEDIA', 'DIFICIL'):
            dif = 'MEDIA'
        if not (enun and 2 <= len(alts) <= 5 and 0 <= correta < len(alts)):
            continue
        item = {'enunciado': enun[:500], 'alternativas': alts[:5],
                'correta': correta, 'dificuldade': dif}
        if com_momento:
            try:
                m = max(0, int(q.get('momento_seg')))
                item['momento_seg'] = min(m, momento_max) if momento_max else m
            except (TypeError, ValueError):
                item['momento_seg'] = None   # ausente -> sem sugestão de momento
        out.append(item)
    return out

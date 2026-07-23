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

MODELO = os.environ.get('TREINO_IA_MODELO', 'claude-sonnet-4-6')
logger = logging.getLogger(__name__)

SYSTEM = (
    'Você cria perguntas de MÚLTIPLA ESCOLHA para treinamento de funcionários '
    'de padaria/alimentação, em português correto do Brasil. A partir do '
    'CONTEÚDO fornecido, gere perguntas objetivas, claras e sem pegadinha, cada '
    'uma com 4 alternativas plausíveis e UMA correta. Responda SÓ com JSON: '
    'uma lista de objetos {"enunciado": str, "alternativas": [str, str, str, '
    'str], "correta": int (índice 0-3 da certa), "dificuldade": '
    '"FACIL"|"MEDIA"|"DIFICIL"}. Sem texto fora do JSON.')


def gerar(texto, n=5):
    """Propõe até `n` perguntas a partir de `texto`. Retorna
    {'perguntas': [...], 'modelo_usado': ...} ou {'erro': ...}. NÃO salva nada
    (revisão humana acontece na tela antes de gravar)."""
    texto = (texto or '').strip()
    if len(texto) < 40:
        return {'erro': 'Cole um conteúdo maior (roteiro/resumo do vídeo) '
                        'pra IA ter do que tirar as perguntas.'}
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return {'erro': 'ANTHROPIC_API_KEY não configurada.'}
    try:
        import anthropic
    except ImportError:
        return {'erro': 'biblioteca anthropic não instalada.'}
    n = max(1, min(int(n or 5), 15))
    instrucao = (f'Gere {n} pergunta(s) a partir deste conteúdo:\n\n'
                 f'{texto[:8000]}')
    client = anthropic.Anthropic(api_key=api_key, timeout=120, max_retries=1)
    try:
        resp = client.messages.create(
            model=MODELO, max_tokens=3000, system=SYSTEM,
            messages=[{'role': 'user', 'content': instrucao}])
        from app.services import uso_ia
        uso_ia.registrar('treino_ia_perguntas', MODELO,
                         getattr(resp, 'usage', None))
        bruto = ''.join(b.text for b in resp.content
                        if getattr(b, 'type', '') == 'text')
        bruto = re.sub(r'^```(?:json)?\s*|\s*```$', '', bruto.strip(),
                       flags=re.MULTILINE)
        dados = json.loads(bruto)
    except json.JSONDecodeError:
        return {'erro': 'a IA devolveu resposta inválida — tente de novo.'}
    except Exception as exc:  # noqa: BLE001 — falha de rede/modelo é reportada
        logger.warning('treino_ia_perguntas: falha: %s', exc)
        return {'erro': f'falha na IA: {exc}'}
    perguntas = _sanitizar(dados)
    if not perguntas:
        return {'erro': 'a IA não retornou perguntas utilizáveis.'}
    return {'perguntas': perguntas, 'modelo_usado': MODELO}


def _sanitizar(dados):
    """Blinda a proposta da IA: enunciado não-vazio, 2-5 alternativas, índice
    correto válido. Descarta o que não bate."""
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
        if enun and 2 <= len(alts) <= 5 and 0 <= correta < len(alts):
            out.append({'enunciado': enun[:500], 'alternativas': alts[:5],
                        'correta': correta, 'dificuldade': dif})
    return out

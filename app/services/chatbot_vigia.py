"""Vigia do chatbot — IA supervisora que assiste cada conversa do bot e alerta
o dono via WhatsApp (Z-API) quando detecta problema.

Roda DEPOIS de o bot ter respondido (nao atrasa o cliente). Usa Claude Haiku
4.5 (modelo barato e rapido, suficiente pra classificacao): ~$0.003 por
avaliacao = ~R$9/mes pro volume atual.

Detecta principalmente:
- Cliente irritado/frustrado/prestes a desistir
- Bot afirmando "esgotado"/"nao tem" produto que TEM em alguma loja fisica
  (passamos EstoqueLoja no contexto pra o supervisor cruzar)
- Handoff feito quando o bot poderia ter resolvido
- Possivel perda de venda
- Bot afirmando algo claramente errado (preco estranho, info inventada)

Anti-spam: so dispara WhatsApp se gravidade for `alta` ou `media`. Casos
`baixa`/sem alerta ficam so no log (visiveis em /admin/debug-schema se um
dia adicionarmos dashboard).
"""
import json
import logging
import os

from flask import current_app

logger = logging.getLogger(__name__)

MODELO = 'claude-haiku-4-5-20251001'
MAX_TOKENS = 400

PROMPT_VIGIA = """Você é o Vigia: supervisor automático do bot de atendimento da O Pão (padaria artesanal).
Sua função é ler a conversa abaixo e decidir se o dono precisa ser AVISADO no WhatsApp.

ALERTE (gravidade=alta) quando:
- Cliente IRRITADO, FRUSTRADO, ofendido ou prestes a desistir/cancelar
- Bot afirmou "esgotado"/"não temos"/"sem estoque" para item que CONSTA no estoque interno abaixo (ERRO REAL)
- Bot disse algo claramente errado (preço estranho, prazo errado, info inventada, contradição grave)
- Possível PERDA DE VENDA confirmada (cliente perguntou, bot não atendeu, cliente saiu)

ALERTE (gravidade=media) quando:
- Handoff feito por algo que parecia simples e o bot poderia ter resolvido
- Cliente confuso depois de várias trocas sem progresso
- Bot citou produto/preço duvidoso mas sem ERRO claro

NÃO alerte quando:
- Conversa fluindo, cliente satisfeito ou neutro
- Handoff CORRETO (entrega/CEP/reagendar pedido, reclamação grave, pedido de humano)
- Cliente apenas tirou dúvida e foi atendido

Seja RIGOROSO: alerta demais vira ruído e o dono para de ler. Em dúvida, NÃO alerte.

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


def _resumo_estoque_loja(limite=80):
    """Resumo dos itens com saldo positivo em alguma loja. Curto pra caber no
    prompt (uma linha por item, agregado). E o contexto interno que permite o
    Vigia detectar 'bot disse esgotado mas tem na loja'."""
    try:
        from collections import defaultdict

        from app.models import EstoqueLoja
    except Exception:  # noqa: BLE001
        return ''

    saldos = defaultdict(int)
    try:
        for e in EstoqueLoja.query.filter(EstoqueLoja.quantidade > 0).all():
            if e.receita and e.receita.nome:
                saldos[e.receita.nome.strip()] += e.quantidade or 0
            elif e.produto and e.produto.nome:
                saldos[e.produto.nome.strip()] += e.quantidade or 0
            elif (e.nome_pendente or '').strip():
                saldos[e.nome_pendente.strip()] += e.quantidade or 0
    except Exception:  # noqa: BLE001
        logger.exception('vigia: _resumo_estoque_loja falhou')
        return ''

    if not saldos:
        return '(nenhum item com saldo nas lojas agora)'
    itens = sorted(saldos.items(), key=lambda kv: -kv[1])[:limite]
    return '\n'.join(f'- {nome}: {qtd} un' for nome, qtd in itens)


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


def _chamar_haiku(api_key, contexto):
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=MODELO,
        max_tokens=MAX_TOKENS,
        system=PROMPT_VIGIA,
        messages=[{'role': 'user', 'content': contexto}],
    )
    texto = ''.join(b.text for b in resp.content
                    if getattr(b, 'type', None) == 'text' and b.text).strip()
    # Tolerante a markdown wrappers do tipo ```json ... ```
    if texto.startswith('```'):
        texto = texto.split('```', 2)[1]
        if texto.startswith('json'):
            texto = texto[4:].strip()
        texto = texto.rsplit('```', 1)[0].strip()
    return json.loads(texto)


def avaliar(historico, *, conv_id=None, nome_contato='', resultado_bot=None):
    """Avalia a conversa e, se for alerta de gravidade alta/media, envia
    WhatsApp pro dono via Z-API. Best-effort: nunca propaga exception (e
    nunca trava o atendimento, ja que so roda DEPOIS de o bot ter respondido).

    `historico`: lista [{'role', 'content'}] da conversa
    `resultado_bot`: {'acao', 'texto', 'motivo'?} do que o bot acabou de fazer
    """
    if not disponivel():
        return {'pulou': 'vigia desligado'}

    api_key = (os.environ.get('ANTHROPIC_API_KEY')
               or current_app.config.get('ANTHROPIC_API_KEY'))
    if not api_key:
        return {'pulou': 'sem ANTHROPIC_API_KEY'}

    try:
        contexto = (
            f'Cliente: {nome_contato or "(sem nome)"}\n'
            f'Conversation ID: {conv_id or "?"}\n\n'
            f'CONVERSA (últimas mensagens):\n{_formatar_historico(historico)}\n\n'
            f'ÚLTIMA AÇÃO DO BOT: {(resultado_bot or {}).get("acao", "?")} - '
            f'{(resultado_bot or {}).get("motivo", "")}\n\n'
            f'ESTOQUE ATUAL NAS LOJAS (use pra detectar bot dizendo "esgotado" indevidamente):\n'
            f'{_resumo_estoque_loja()}'
        )
        veredicto = _chamar_haiku(api_key, contexto)
    except Exception as exc:  # noqa: BLE001
        logger.exception('vigia: avaliacao falhou')
        return {'erro': str(exc)}

    if not isinstance(veredicto, dict):
        return {'erro': 'veredicto invalido'}

    logger.info('vigia conv=%s veredicto=%s', conv_id, veredicto)

    if not veredicto.get('alerta'):
        return {'silencio': True, 'veredicto': veredicto}
    if veredicto.get('gravidade') not in ('alta', 'media'):
        return {'silencio': True, 'veredicto': veredicto}

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

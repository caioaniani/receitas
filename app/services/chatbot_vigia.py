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
`baixa`/sem alerta ficam so no log e no historico em memoria (rota
/admin/vigia/diag mostra os ultimos).
"""
import json
import logging
import os
from collections import deque

from flask import current_app

logger = logging.getLogger(__name__)

MODELO = 'claude-haiku-4-5-20251001'
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
- Bot afirmou "esgotado"/"não temos" para item que CONSTA no estoque interno abaixo (ERRO REAL)
- Bot disse algo claramente errado: preço estranho, prazo errado, info inventada, contradição grave
- PERDA DE VENDA clara: cliente estava comprando, o bot atrapalhou/confundiu, e o cliente saiu

GRAVIDADE=MEDIA (não urgente — vai pro resumo diário):
- Handoff que o bot PODERIA ter resolvido (ex: "o que tem na cesta?", dúvida simples de produto)
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


def _avaliar_interno(historico, *, conv_id=None, nome_contato='', resultado_bot=None):
    """Logica de avaliar — multiplos returns, sem efeitos colaterais alem
    do envio via Z-API. O wrapper `avaliar` cuida do registro no historico."""
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

    # SO alta dispara WhatsApp na hora. media fica registrado (resumo diario),
    # sem incomodar o dono em tempo real.
    if not veredicto.get('alerta') or veredicto.get('gravidade') != 'alta':
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
        from app.extensions import db
        from app.models import VigiaVeredito
        rb = resultado_bot or {}
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
        _registrar(res, conv_id, nome_contato, ultima_msg)
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


def _chamar_haiku_abandono(api_key, contexto):
    """Igual ao _chamar_haiku, mas com PROMPT_ABANDONO."""
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=MODELO,
        max_tokens=MAX_TOKENS,
        system=PROMPT_ABANDONO,
        messages=[{'role': 'user', 'content': contexto}],
    )
    texto = ''.join(b.text for b in resp.content
                    if getattr(b, 'type', None) == 'text' and b.text).strip()
    if texto.startswith('```'):
        texto = texto.split('```', 2)[1]
        if texto.startswith('json'):
            texto = texto[4:].strip()
        texto = texto.rsplit('```', 1)[0].strip()
    return json.loads(texto)


def avaliar_abandono(historico, *, conv_id=None, nome_contato='', minutos_sem_resposta=0):
    """Avalia conversa PARADA (cliente sumiu) e alerta no WhatsApp se valer.
    Best-effort. Usado pelo cron `_run_vigia_abandono` no seru_cron.py."""
    if not disponivel():
        return {'pulou': 'vigia desligado'}

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
        veredicto = _chamar_haiku_abandono(api_key, contexto)
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

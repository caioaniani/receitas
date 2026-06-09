"""Bot WhatsApp do dono: copilot full read-only via Z-API.

So responde pra o `ZAPI_BOT_DONO_NUMERO` (whitelist hard). Histórico persistente
(`ZapiBotConversa`). Idempotente (`ZapiBotEventoProcessado` por messageId).

Modo: leitura. `copilot.interpretar(apenas_leitura=True)` esconde TODAS as tools
de write do Claude — Claude nem ve elas, nao tem como acionar.

Webhook: POST /zapi/webhook?k=ZAPI_BOT_WEBHOOK_TOKEN (configura no painel Z-API
em 'Ao receber' ou 'On message received').
"""
import base64
import json
import logging

import requests
from flask import current_app

logger = logging.getLogger(__name__)

# Limite do contexto enviado ao Claude. Sonnet aguenta 200k tokens, mas 80
# turnos cobre 99% das conversas e mantem custo controlado. Antigos ficam
# persistidos no banco — so nao vao pra cada chamada.
MAX_HIST_TURNOS = 80


def _so_digitos(s):
    return ''.join(c for c in (s or '') if c.isdigit())


def _numero_dono():
    return _so_digitos(current_app.config.get('ZAPI_BOT_DONO_NUMERO') or '')


def disponivel():
    cfg = current_app.config
    return bool((cfg.get('ZAPI_BOT_DONO_NUMERO') or '').strip()
                and (cfg.get('ZAPI_BOT_WEBHOOK_TOKEN') or '').strip())


def _extrair_texto(payload):
    """Z-API tem N formatos de mensagem. Pega o texto que houver."""
    if not isinstance(payload, dict):
        return ''
    txt = payload.get('text')
    if isinstance(txt, dict):
        msg = txt.get('message')
        if msg:
            return str(msg).strip()
    if isinstance(txt, str):
        return txt.strip()
    # Imagem com legenda
    img = payload.get('image')
    if isinstance(img, dict):
        return str(img.get('caption') or '').strip()
    return ''


def _baixar_imagens(payload):
    """Z-API entrega URLs publicas temporarias pra anexos. Baixa, devolve
    [{'mimetype', 'base64'}] (formato que `copilot.interpretar` espera)."""
    urls = []
    img = payload.get('image') if isinstance(payload, dict) else None
    if isinstance(img, dict) and img.get('imageUrl'):
        urls.append((img['imageUrl'], img.get('mimeType') or 'image/jpeg'))
    out = []
    for url, mime in urls[:3]:   # limite defensivo
        try:
            r = requests.get(url, timeout=10)
            if r.status_code != 200:
                continue
            out.append({'mimetype': mime,
                        'base64': base64.b64encode(r.content).decode('ascii')})
        except requests.RequestException:
            logger.warning('zapi_bot: download imagem falhou %s', url)
    return out


def _user_dono():
    """Pega o User dono (is_owner=True). 1 esperado."""
    from app.models import Usuario
    return Usuario.query.filter_by(is_owner=True).first()


def _conversa_dono():
    """Singleton da conversa (1 linha por telefone do dono)."""
    from app.extensions import db
    from app.models import ZapiBotConversa
    tel = _numero_dono()
    if not tel:
        return None
    conv = ZapiBotConversa.query.filter_by(telefone=tel).first()
    if not conv:
        conv = ZapiBotConversa(telefone=tel, mensagens_json='[]')
        db.session.add(conv)
        db.session.commit()
    return conv


def _historico(conv):
    try:
        return json.loads(conv.mensagens_json or '[]')
    except json.JSONDecodeError:
        return []


def _salvar_turno(conv, role, content, imagens_count=0):
    from app.extensions import db
    hist = _historico(conv)
    entry = {'role': role, 'content': content}
    if imagens_count:
        entry['imagens_count'] = imagens_count
    hist.append(entry)
    conv.mensagens_json = json.dumps(hist, ensure_ascii=False)
    db.session.commit()


def processar_payload(payload):
    """Trata UM webhook do Z-API. Disparado async (thread). Idempotente."""
    if not isinstance(payload, dict):
        return
    if payload.get('fromMe'):
        return  # ignora mensagens que o proprio bot mandou
    if payload.get('isGroup'):
        return  # so atende DM 1-1
    phone_remetente = _so_digitos(payload.get('phone') or '')
    if not phone_remetente or phone_remetente != _numero_dono():
        logger.info('zapi_bot: ignorado numero %r (nao eh dono)',
                    phone_remetente[-4:] if phone_remetente else '')
        return

    message_id = str(payload.get('messageId') or '').strip()
    if not message_id:
        return

    from app.extensions import db
    from app.models import ZapiBotEventoProcessado

    if ZapiBotEventoProcessado.query.get(message_id):
        return
    db.session.add(ZapiBotEventoProcessado(message_id=message_id))
    try:
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        return  # alguem mais ja processou

    texto = _extrair_texto(payload)
    imagens = _baixar_imagens(payload)
    if not texto and not imagens:
        return

    user = _user_dono()
    if not user:
        logger.warning('zapi_bot: nenhum Usuario com papel=owner ativo no banco')
        _responder('Bot offline: nao achei usuario owner pra autorizar consultas.')
        return

    conv = _conversa_dono()
    if not conv:
        return
    historico = _historico(conv)[-MAX_HIST_TURNOS:]

    from app.services import copilot as copilot_svc
    try:
        resp = copilot_svc.interpretar(
            texto or '(imagem enviada)',
            user,
            historico=historico,
            images=imagens or None,
            apenas_leitura=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception('zapi_bot: copilot.interpretar falhou')
        _responder('Tive um erro interno aqui. Tenta de novo daqui a pouco.')
        return

    resposta_texto = _formatar_resposta(resp)
    if not resposta_texto:
        resposta_texto = 'Desculpa, não consegui formular uma resposta.'

    # Persistencia: salva user PRIMEIRO, depois assistant — assim na proxima msg
    # o historico ja vem coerente
    _salvar_turno(conv, 'user', texto or '(imagem)',
                  imagens_count=len(imagens))
    _salvar_turno(conv, 'assistant', resposta_texto)

    _responder(resposta_texto)


def _formatar_resposta(resp):
    """Converte a dict de retorno do copilot em texto pra WhatsApp.

    Em modo leitura, dois caminhos possiveis:
      - tipo='conversa'        → so explicacao (Claude conversando)
      - tipo='consultar_*'     → tool de read ja executou; resultado.texto
                                 traz o que devolver pra o user (igual Slack)."""
    if not isinstance(resp, dict):
        return ''
    tipo = resp.get('tipo')
    explicacao = (resp.get('explicacao') or '').strip()
    if tipo == 'erro':
        return f'Erro: {explicacao or "indeterminado"}'
    if tipo == 'conversa':
        return explicacao or '(sem resposta)'
    resultado = resp.get('resultado') or {}
    if isinstance(resultado, dict):
        if resultado.get('erro'):
            return f'Erro: {resultado["erro"]}'
        return resultado.get('texto') or explicacao or '(sem detalhes)'
    return explicacao


def _responder(texto):
    from app.services import zapi
    numero = _numero_dono()
    if not numero or not texto:
        return
    try:
        zapi.enviar_texto(numero, texto)
    except Exception:  # noqa: BLE001
        logger.exception('zapi_bot: enviar_texto falhou')

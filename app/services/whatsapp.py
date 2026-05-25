"""Servico de notificacao WhatsApp: envia via Z-API e REGISTRA cada envio
(NotificacaoWhatsapp), e o motor das automacoes agendadas (AutomacaoWhatsapp)."""
import logging

from flask import current_app

from app.extensions import db
from app.models import AutomacaoWhatsapp, NotificacaoWhatsapp
from app.utils import agora

logger = logging.getLogger(__name__)


def notificar(numero, mensagem, origem='manual'):
    """Envia uma mensagem pelo Z-API e registra no log (NotificacaoWhatsapp).
    Retorna o dict de zapi.enviar_texto."""
    from app.services import zapi
    res = zapi.enviar_texto(numero, mensagem)
    try:
        db.session.add(NotificacaoWhatsapp(
            numero=numero, mensagem=mensagem, origem=origem,
            ok=bool(res.get('ok')), erro=(res.get('erro') or '')[:300]))
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception('whatsapp: falha ao registrar log de envio')
    return res


def disparar_automacoes_devidas():
    """Motor (job mestre): dispara as automacoes ativas cujo horario ja chegou
    hoje, no dia da semana permitido, e que ainda nao dispararam hoje. Marca
    ultimo_disparo_em ANTES de enviar (evita disparo duplicado). Retorna nº
    de automacoes disparadas."""
    agora_dt = agora()
    hoje_d = agora_dt.date()
    destino_padrao = (current_app.config.get('ZAPI_NUMERO_DESTINO') or '').strip()
    n = 0
    for a in AutomacaoWhatsapp.query.filter_by(ativo=True).all():
        if agora_dt.weekday() not in a.dias_set:
            continue
        try:
            hh, mm = (a.horario or '').split(':')
            alvo = agora_dt.replace(hour=int(hh), minute=int(mm),
                                    second=0, microsecond=0)
        except (ValueError, AttributeError):
            continue
        if agora_dt < alvo:
            continue                                  # ainda nao deu o horario
        if a.ultimo_disparo_em and a.ultimo_disparo_em.date() >= hoje_d:
            continue                                  # ja disparou hoje
        destino = (a.destino or '').strip() or destino_padrao
        if not destino:
            continue
        a.ultimo_disparo_em = agora_dt                # marca antes de enviar
        db.session.commit()
        notificar(destino, a.mensagem, origem=f'automacao:{a.id}')
        n += 1
    return n

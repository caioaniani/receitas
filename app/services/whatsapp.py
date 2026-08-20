"""Servico de notificacao WhatsApp: envia via Z-API e REGISTRA cada envio
(NotificacaoWhatsapp), e o motor das automacoes agendadas (AutomacaoWhatsapp)."""
import logging

from flask import current_app

from app.extensions import db
from app.models import AutomacaoWhatsapp, NotificacaoWhatsapp
from app.utils import agora

logger = logging.getLogger(__name__)


def claim_envio(chave, tick_id):
    """Claim persistente ANTI-DUPLICATA de envio agendado (19-20/08/2026,
    dono: "Continua duplicando"): num deploy, o container velho e o novo
    ficam vivos juntos por alguns minutos e os DOIS disparam o cron do
    minuto — o advisory lock so serializa execucoes SIMULTANEAS; a segunda,
    segundos depois, pega o lock livre e reenvia. O mesmo vale pros DOIS
    workers gunicorn do MESMO container (cada um roda seu APScheduler).

    Grava e COMMITA o marcador em AppConfig ANTES do envio — quem chegar
    depois com o MESMO tick_id pula. Retorna ('ok', valor_anterior) quando
    o claim e nosso, ('duplicata', None) quando outro processo ja enviou
    este tick, ('erro', None) quando nao deu pra gravar — sem claim duravel
    NAO se envia (duplicar lembrete e pior que atrasar)."""
    from app.models import AppConfig
    try:
        atual = AppConfig.get(chave)
        if atual == tick_id:
            return 'duplicata', None
        AppConfig.set(chave, tick_id)
        db.session.commit()
        return 'ok', atual
    except Exception:  # noqa: BLE001 — claim indisponivel = nao envia
        logger.exception('whatsapp: claim %s falhou', chave)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 'erro', None


def devolver_claim(chave, anterior):
    """Devolve o claim quando o ENVIO falhou (Slack/Z-API fora) — o proximo
    tick/dia nao fica bloqueado por mensagem que nunca saiu. Best-effort:
    falha aqui so deixa o claim "gasto" (perde 1 envio, volta no proximo
    tick/dia)."""
    from app.models import AppConfig
    try:
        AppConfig.set(chave, anterior)
        db.session.commit()
    except Exception:  # noqa: BLE001
        logger.exception('whatsapp: devolver claim %s falhou', chave)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


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

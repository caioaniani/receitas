"""Claim persistente ANTES do envio — fonte única do anti-duplicata (20/08/2026).

POR QUE (dono: "Voce precisa resolver esse duplo texto do bot, muito serio"):
alerta automático duplicado tem sempre a mesma raiz — DOIS processos rodando
o mesmo job e nenhum registro durável dizendo "eu já mandei". Os processos
podem ser:
  1. os 2 workers gunicorn (Procfile: `--workers 2`), cada um com o seu
     APScheduler — job de INTERVALO deriva entre eles e os disparos caem em
     segundos/minutos diferentes, então o advisory lock (que só serializa o
     que é SIMULTÂNEO) fica livre pro segundo;
  2. container velho + novo convivendo no minuto de um deploy;
  3. job sem lock nenhum (era o caso do heartbeat das 08:00, que postava 2x
     por dia no Slack desde sempre).
(Instância COPIADA — homologação com as mesmas envs — é outro problema: o
claim é por BANCO e não cruza instâncias; isso quem resolve é
`app/services/instancia.py`.)

REGRA: grave o claim e COMMITE **antes** de enviar; se o envio falhar,
DEVOLVA o claim (`devolver`) pra retentar no próximo ciclo. A janela que
sobra — processo morto entre o claim e o envio — perde UM alerta, e esse é
o trade-off aceito: aqui duplicar incomoda mais que atrasar.
"""
import logging

logger = logging.getLogger(__name__)


def claim(chave, tick):
    """Reserva o envio identificado por (chave, tick).

    `tick` é o que define "é o mesmo envio": um dia (`2026-08-20`) pra
    alerta diário, um minuto (`2026-08-20T20:10`) pra escalada de vários
    ticks, um período qualquer — desde que dois processos do MESMO envio
    calculem o mesmo valor.

    Retorna `(status, anterior)`:
      - `('ok', valor_antigo)` — o claim é seu, pode enviar;
      - `('duplicata', None)` — outro processo já enviou este tick;
      - `('erro', None)` — não deu pra gravar; NÃO envie (sem registro
        durável o próximo ciclo duplicaria).
    """
    from app.extensions import db
    from app.models import AppConfig
    try:
        atual = AppConfig.get(chave)
        if atual == tick:
            return 'duplicata', None
        AppConfig.set(chave, tick)
        db.session.commit()
        return 'ok', atual
    except Exception:  # noqa: BLE001 — sem claim durável não se envia
        logger.exception('alerta_claim: claim %s falhou', chave)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 'erro', None


def devolver(chave, anterior):
    """Devolve o claim quando o ENVIO falhou (Slack/Z-API fora), pra não
    bloquear a retentativa. Best-effort: falhar aqui só mantém o claim
    'gasto' — perde-se um alerta, nunca duplica."""
    from app.extensions import db
    from app.models import AppConfig
    try:
        AppConfig.set(chave, anterior)
        db.session.commit()
    except Exception:  # noqa: BLE001
        logger.exception('alerta_claim: devolver %s falhou', chave)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


def claim_por_cooldown(chave, segundos):
    """Variante por TEMPO, pra job de INTERVALO (que não tem um "tick"
    redondo): só libera se o último claim tiver mais de `segundos`. É o que
    impede o segundo worker — cujo intervalo derivou alguns minutos — de
    repetir o ciclo inteiro do vigia.

    Retorna True se pode rodar. Erro de banco = True (fail-open: guarda
    quebrada não pode calar vigia de produção).
    """
    from datetime import timedelta

    from app.extensions import db
    from app.models import AppConfig
    from app.utils import agora
    try:
        agora_dt = agora()
        bruto = AppConfig.get(chave)
        if bruto:
            try:
                from datetime import datetime
                ultimo = datetime.fromisoformat(bruto)
                if agora_dt - ultimo < timedelta(seconds=segundos):
                    return False
            except (TypeError, ValueError):
                pass          # valor ilegível: trata como sem claim
        AppConfig.set(chave, agora_dt.isoformat())
        db.session.commit()
        return True
    except Exception:  # noqa: BLE001 — fail-open
        logger.exception('alerta_claim: cooldown %s falhou', chave)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return True

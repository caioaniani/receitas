"""Dedupe do TEMPLATE de WhatsApp disparado pelo painel (item 10 da spec do
dono, caso E3862E49, 22/09/2026).

Caso real (conv 2429): a equipe clicou "Chamar" QUATRO vezes no mesmo
pedido em duas horas porque a tela dizia "template enviado" e nada mostrava
a recusa da Meta. Cada clique e um template cobrado e, quando chega, uma
mensagem repetida ao cliente. Regra: o MESMO template para o MESMO destino
sobre a MESMA referencia (codigo do pedido / assunto) so sai uma vez a cada
`JANELA_MIN` minutos; a tentativa seguinte devolve ao painel que ja foi
enviado e quando (`resposta_bloqueio`), com a conversa pra abrir.

Registro em `TemplateWhatsappEnvio` (tabela nova, `db.create_all`). Grava
SO envio que o Chatwoot aceitou (`ok=True`) — template recusado nao conta,
senao a equipe ficaria 60 min sem poder tentar de novo.
"""
import logging
from datetime import timedelta

from app.extensions import db
from app.models import TemplateWhatsappEnvio
from app.utils import agora, telefone_chave

logger = logging.getLogger(__name__)

JANELA_MIN = 60


def _chave(telefone):
    """Chave canonica do destino: `telefone_chave` (DDD + 8 digitos) e, pra
    numero fora do padrao BR, os digitos crus."""
    chave = telefone_chave(telefone or '')
    if chave:
        return chave
    return ''.join(ch for ch in str(telefone or '') if ch.isdigit())[:40]


def _referencia(ref):
    return str(ref or '').strip()[:60]


def envio_recente(telefone, template, referencia, janela_min=JANELA_MIN):
    """Ultimo envio do mesmo (destino, template, referencia) dentro da
    janela, ou None. Comparacao da referencia sem caixa."""
    chave = _chave(telefone)
    ref = _referencia(referencia)
    if not chave or not ref:
        return None
    corte = agora() - timedelta(minutes=janela_min)
    return (TemplateWhatsappEnvio.query
            .filter(TemplateWhatsappEnvio.destino_chave == chave,
                    TemplateWhatsappEnvio.template == (template or '')[:120],
                    db.func.lower(TemplateWhatsappEnvio.referencia) == ref.lower(),
                    TemplateWhatsappEnvio.criado_em >= corte)
            .order_by(TemplateWhatsappEnvio.criado_em.desc())
            .first())


def registrar(telefone, template, referencia, *, conversation_id=None,
              usuario_id=None):
    """Grava o envio aceito. Best-effort: falha aqui NUNCA derruba a rota —
    o template ja saiu, e o pior caso e um reenvio permitido cedo demais."""
    chave = _chave(telefone)
    ref = _referencia(referencia)
    if not chave or not ref:
        return None
    try:
        conv = int(conversation_id) if conversation_id is not None else None
    except (TypeError, ValueError):
        conv = None
    try:
        row = TemplateWhatsappEnvio(
            destino_chave=chave, template=(template or '')[:120], referencia=ref,
            conversation_id=conv, usuario_id=usuario_id)
        db.session.add(row)
        db.session.commit()
        return row
    except Exception:  # noqa: BLE001
        db.session.rollback()
        logger.exception('template_dedupe: registrar falhou (destino=%s ref=%s)',
                         chave, ref)
        return None


def resposta_bloqueio(row, nome=None):
    """Payload que a rota devolve (HTTP 409) quando o template ja saiu na
    janela: diz QUANDO, ha quanto tempo e leva a conversa pra abrir."""
    quando = row.criado_em
    minutos = max(0, int((agora() - quando).total_seconds() // 60))
    faltam = max(1, JANELA_MIN - minutos)
    erro = (f'Modelo já enviado às {quando:%H:%M} (há {minutos} min) para este '
            f'número sobre {row.referencia} — não reenviado. Responda pela '
            f'conversa; um novo envio só depois de {faltam} min.')
    return {
        'ok': False,
        'erro': erro,
        'ja_enviado': True,
        'ja_enviado_em': quando.isoformat(),
        'ha_minutos': minutos,
        'conversation_id': row.conversation_id,
        'nova': False,
        'aberta': None,
        'nome': nome,
    }

"""Presença humana numa conversa do Chatwoot — nota privada cala o bot.

Regra do dono (20/09/2026, caso conv 2409): "o bot não pode falar quando a
gente fala no privado com a equipe". Naquele caso ele reabriu a conversa
do entregador e escreveu a nota privada "@Painel" às 19:06; às 19:20 o
sistema mandou ao contato o texto automático de contenção da espera
humana ("equipe em alta demanda"). A nota privada é o sinal de que a
equipe tomou a conversa — a partir dela NENHUMA fala automática ao
contato pode sair: resposta do bot, resposta a áudio, follow-up,
vassoura, contenção.

FONTE ÚNICA: `humano_presente(conv_id)`. Todo caminho que fala com o
contato consulta aqui. O marcador é gravado pelo webhook do Agent Bot
(`crm/routes.bot_webhook`) quando chega `message_created` com
`private=true` e remetente HUMANO (`chatwoot.remetente_humano`) — o bot e
as automações do Chatwoot não contam; o nosso sistema nunca escreve nota
privada (`enviar_mensagem_painel` posta mensagem pública).

Validade: `PRESENCA_HUMANA_HORAS`. A conversa resolvida e reaberta pelo
cliente depois da janela volta ao bot normalmente; dentro da janela o bot
segue em silêncio e a conversa vai para a fila humana (open). Errar para
o lado do silêncio é a direção pedida — a fila humana + a cobrança de
espera garantem que ninguém fica no vácuo.

`consultar_chatwoot=True` (só na contenção, 1 GET por incidente): rede de
segurança caso o webhook da nota não tenha chegado — lê as mensagens da
conversa e procura nota privada humana recente; se achar, persiste.
"""
import logging
from datetime import timedelta

from app.extensions import db
from app.models import PresencaHumanaConversa
from app.utils import agora

logger = logging.getLogger(__name__)

PRESENCA_HUMANA_HORAS = 12


def registrar_nota_privada(conv_id, autor=None, *, quando=None):
    """Upsert do marcador. `quando` (BRT naive) permite registrar a nota
    lida da API com o instante real dela; default = agora. Nunca levanta:
    falha aqui não pode derrubar o webhook (o marcador é best-effort, a
    rede de segurança da contenção relê a API)."""
    conv_id = str(conv_id or '').strip()
    if not conv_id:
        return None
    quando = quando or agora()
    try:
        row = db.session.get(PresencaHumanaConversa, conv_id)
        if row is None:
            row = PresencaHumanaConversa(conv_id=conv_id, nota_em=quando,
                                         autor=(autor or '')[:120] or None,
                                         notas=1)
            db.session.add(row)
        else:
            if quando >= (row.nota_em or quando):
                row.nota_em = quando
                if autor:
                    row.autor = autor[:120]
            row.notas = (row.notas or 0) + 1
        db.session.commit()
        return row
    except Exception:  # noqa: BLE001
        db.session.rollback()
        logger.exception('presenca_humana: registrar falhou conv=%s', conv_id)
        return None


def marcar_aberta(conv_id):
    """Registra que o sistema tirou a conversa do bot por causa da nota."""
    try:
        row = db.session.get(PresencaHumanaConversa, str(conv_id))
        if row is not None and not row.aberta_em:
            row.aberta_em = agora()
            db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        logger.exception('presenca_humana: marcar_aberta falhou conv=%s', conv_id)


def ultima_nota(conv_id):
    """Instante (BRT naive) da última nota privada humana registrada, ou None."""
    try:
        row = db.session.get(PresencaHumanaConversa, str(conv_id))
    except Exception:  # noqa: BLE001
        logger.exception('presenca_humana: leitura falhou conv=%s', conv_id)
        return None
    return row.nota_em if row is not None else None


def humano_presente(conv_id, *, horas=PRESENCA_HUMANA_HORAS,
                    consultar_chatwoot=False):
    """True se um humano escreveu nota privada nesta conversa nas últimas
    `horas`. Com `consultar_chatwoot=True`, sem marcador (ou marcador
    velho) relê a API do Chatwoot como rede de segurança e persiste o que
    achar. Falha de leitura = False (fail-open para a fala automática:
    o que barra é o SINAL, nunca a ausência de sinal por erro)."""
    if not conv_id:
        return False
    corte = agora() - timedelta(hours=horas)
    quando = ultima_nota(conv_id)
    if quando is not None and quando >= corte:
        return True
    if not consultar_chatwoot:
        return False
    try:
        from app.services import chatwoot
        na_api = chatwoot.nota_privada_humana_recente(conv_id, horas=horas)
    except Exception:  # noqa: BLE001
        logger.exception('presenca_humana: consulta ao Chatwoot falhou conv=%s',
                         conv_id)
        return False
    if na_api is None:
        return False
    registrar_nota_privada(conv_id, na_api.get('autor'), quando=na_api.get('quando'))
    return True

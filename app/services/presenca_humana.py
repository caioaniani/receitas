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
as automações do Chatwoot não contam; a nota do PRÓPRIO bot
(`chatwoot.enviar_nota_privada`, prefixo `entrega_candidata.PREFIXO_NOTA_BOT`)
também não.

"Quando a gente fala" = ENQUANTO o episódio humano dura. O marcador é
LIMPO (`limpar`) quando um humano encerra ou devolve a conversa: status
`resolved` ou `pending` visto pelo webhook (`conversation_status_changed`
/`conversation_resolved`, se o Chatwoot os entregar ao Agent Bot), botão
"Devolvida pro bot"/resolver do painel de entregas e a leitura de
`resolved` em `atendimento_pendente.candidatos`. Como esses sinais podem
não chegar, a janela `PRESENCA_HUMANA_HORAS` é o teto: passada, o bot
volta. Dentro dela, sem sinal de encerramento, errar para o silêncio é a
direção pedida — a conversa vai para a fila humana, não para o vácuo
(revisão 21/09/2026: o gesto "Devolvida pro bot" e o resolve+reabre
tinham ficado presos pelos 12h da 1ª versão).

`consultar_chatwoot=True` (contenção e vassoura, 1 GET): rede de
segurança caso o webhook da nota não tenha chegado — lê as mensagens da
conversa e procura nota privada humana recente; se achar, persiste.

Escritas em SESSÃO ISOLADA (`Session(db.engine)`, padrão do `uso_ia`):
`registrar_nota_privada` é chamado de dentro de leitores
(`chatwoot.buscar_historico`) e nunca pode commitar/reverter a transação
de quem chamou.
"""
import logging
from datetime import timedelta

from sqlalchemy.orm import Session

from app.extensions import db
from app.models import PresencaHumanaConversa
from app.utils import agora

logger = logging.getLogger(__name__)

PRESENCA_HUMANA_HORAS = 6


def registrar_nota_privada(conv_id, autor=None, *, quando=None):
    """Upsert do marcador. `quando` (BRT naive) permite registrar a nota
    lida da API com o instante real dela; default = agora. `notas` só
    cresce quando a nota é MAIS NOVA que a registrada (a mesma nota lida
    de novo pela API não conta duas vezes). Nunca levanta: falha aqui não
    pode derrubar o webhook (o marcador é best-effort; a rede de segurança
    relê a API). Devolve o instante registrado ou None."""
    conv_id = str(conv_id or '').strip()
    if not conv_id:
        return None
    quando = quando or agora()
    try:
        with Session(db.engine) as s:
            row = s.get(PresencaHumanaConversa, conv_id)
            if row is None:
                row = PresencaHumanaConversa(conv_id=conv_id, nota_em=quando,
                                             autor=(autor or '')[:120] or None,
                                             notas=1)
                s.add(row)
            elif quando > (row.nota_em or quando):
                row.nota_em = quando
                row.notas = (row.notas or 0) + 1
                if autor:
                    row.autor = autor[:120]
            s.commit()
            return row.nota_em
    except Exception:  # noqa: BLE001
        logger.exception('presenca_humana: registrar falhou conv=%s', conv_id)
        return None


def marcar_aberta(conv_id):
    """Registra que o sistema tirou a conversa do bot por causa da nota."""
    try:
        with Session(db.engine) as s:
            row = s.get(PresencaHumanaConversa, str(conv_id))
            if row is not None and not row.aberta_em:
                row.aberta_em = agora()
                s.commit()
    except Exception:  # noqa: BLE001
        logger.exception('presenca_humana: marcar_aberta falhou conv=%s', conv_id)


def limpar(conv_id, motivo=''):
    """Encerra o episódio humano (resolvida / devolvida ao bot): apaga o
    marcador. Idempotente; nunca levanta. Devolve True se havia marcador."""
    conv_id = str(conv_id or '').strip()
    if not conv_id:
        return False
    try:
        with Session(db.engine) as s:
            row = s.get(PresencaHumanaConversa, conv_id)
            if row is None:
                return False
            s.delete(row)
            s.commit()
        logger.info('presenca_humana: marcador limpo conv=%s (%s)', conv_id, motivo or '?')
        return True
    except Exception:  # noqa: BLE001
        logger.exception('presenca_humana: limpar falhou conv=%s', conv_id)
        return False


def ultima_nota(conv_id):
    """Instante (BRT naive) da última nota privada humana registrada, ou None."""
    try:
        with Session(db.engine) as s:
            row = s.get(PresencaHumanaConversa, str(conv_id))
            return row.nota_em if row is not None else None
    except Exception:  # noqa: BLE001
        logger.exception('presenca_humana: leitura falhou conv=%s', conv_id)
        return None


def humano_presente(conv_id, *, horas=PRESENCA_HUMANA_HORAS,
                    consultar_chatwoot=False):
    """True se um humano escreveu nota privada nesta conversa nas últimas
    `horas` e o episódio não foi encerrado (`limpar`). Com
    `consultar_chatwoot=True`, sem marcador (ou marcador velho) relê a API
    do Chatwoot como rede de segurança e persiste o que achar. Falha de
    leitura = False (fail-open para a fala automática: o que barra é o
    SINAL, nunca a ausência de sinal por erro)."""
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

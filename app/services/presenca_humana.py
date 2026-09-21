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
também não; nota em conversa já RESOLVIDA é registro, não presença.

"Quando a gente fala" = ENQUANTO o episódio humano dura. O episódio é
ENCERRADO (`encerrar`) quando um humano resolve ou devolve a conversa ao
bot: status `resolved`/`pending` visto pelo webhook
(`conversation_status_changed`, se o Chatwoot o entregar ao Agent Bot),
botões do painel de entregas e a leitura de `resolved` em
`atendimento_pendente.candidatos`. Encerrado = `notas == 0` com
`nota_em` guardando a ÚLTIMA nota conhecida (sem coluna nova: a tabela já
está em produção); só nota MAIS NOVA que `nota_em` reabre o episódio — a
mesma nota relida pela API nunca reabre. `encerrar` sem linha CRIA a
linha encerrada (senão a releitura da API reabria contra o gesto —
revisão 21/09, 2ª rodada). Como os sinais de encerramento podem não
chegar, `PRESENCA_HUMANA_HORAS` é o teto: passada a janela o bot volta.
Dentro dela, sem sinal, errar para o silêncio é a direção pedida — a
conversa vai para a fila humana, não para o vácuo.

Instante da nota: o `created_at` do Chatwoot (mesma fonte do webhook e da
listagem) — comparar `agora()` do nosso relógio com o deles reabria
episódio pela mesma nota e contava reentrega do webhook como nota nova.

`consultar_chatwoot=True` (contenção e vassoura, 1 GET): rede de
segurança caso o webhook da nota não tenha chegado — lê as mensagens da
conversa e procura nota privada humana recente; se achar, persiste. Sinal
na mão + banco falhando = presença (o erro nunca libera a fala).

Escritas em SESSÃO ISOLADA (`Session(db.engine)`, padrão do `uso_ia`):
`registrar_nota_privada` é chamado de dentro de leitores
(`chatwoot.buscar_historico`) e nunca pode commitar/reverter a transação
de quem chamou. Leituras usam a sessão principal (sem escrita, sem
conexão extra no caminho quente).
"""
import logging
from datetime import timedelta

from sqlalchemy.orm import Session

from app.extensions import db
from app.models import PresencaHumanaConversa
from app.utils import agora

logger = logging.getLogger(__name__)

PRESENCA_HUMANA_HORAS = 6
# Evento de status `pending` entregue ATRASADO pelo Chatwoot (job separado)
# pode chegar depois da nota e encerrar o episódio recém-aberto; evento
# que alcança uma nota mais nova que isto é ignorado.
ENCERRAR_TOLERANCIA_SEG = 90


def registrar_nota_privada(conv_id, autor=None, *, quando=None):
    """Upsert do marcador. `quando` (BRT naive) = instante da nota
    (`created_at` do Chatwoot); default = agora. Só nota MAIS NOVA que a
    registrada conta (reabre episódio encerrado e incrementa `notas`); a
    mesma nota relida ou reentregue não conta duas vezes. Nunca levanta.
    Devolve True (nota nova registrada), False (não é mais nova) ou None
    (falha de banco — o chamador com o sinal na mão trata como presença)."""
    conv_id = str(conv_id or '').strip()
    if not conv_id:
        return False
    quando = quando or agora()
    try:
        with Session(db.engine) as s:
            row = s.get(PresencaHumanaConversa, conv_id)
            if row is None:
                s.add(PresencaHumanaConversa(conv_id=conv_id, nota_em=quando,
                                             autor=(autor or '')[:120] or None,
                                             notas=1))
            elif row.nota_em is None or quando > row.nota_em:
                row.nota_em = quando
                row.notas = (row.notas or 0) + 1
                if autor:
                    row.autor = autor[:120]
            else:
                return False
            s.commit()
            return True
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


def encerrar(conv_id, motivo='', *, tolerancia_seg=0):
    """Encerra o episódio humano (conversa resolvida / devolvida ao bot):
    `notas` vira 0 e `nota_em` fica como a última nota conhecida — só nota
    mais nova reabre. Sem linha, CRIA a linha encerrada com `nota_em` =
    agora (a releitura da API não reabre pela nota antiga). Com
    `tolerancia_seg`, nota mais nova que isso NÃO é encerrada (evento de
    status atrasado). `aberta_em` fica (trilha). Idempotente; nunca
    levanta. True se havia episódio aberto e foi encerrado."""
    conv_id = str(conv_id or '').strip()
    if not conv_id:
        return False
    try:
        with Session(db.engine) as s:
            row = s.get(PresencaHumanaConversa, conv_id)
            if row is None:
                s.add(PresencaHumanaConversa(conv_id=conv_id, nota_em=agora(), notas=0))
                s.commit()
                return False
            if not row.notas:
                return False
            if (tolerancia_seg and row.nota_em
                    and row.nota_em >= agora() - timedelta(seconds=tolerancia_seg)):
                logger.info('presenca_humana: encerrar ignorado conv=%s (%s) — nota '
                            'mais nova que %ss', conv_id, motivo or '?', tolerancia_seg)
                return False
            row.notas = 0
            s.commit()
        logger.info('presenca_humana: episodio encerrado conv=%s (%s)',
                    conv_id, motivo or '?')
        return True
    except Exception:  # noqa: BLE001
        logger.exception('presenca_humana: encerrar falhou conv=%s', conv_id)
        return False


def estado(conv_id):
    """(nota_em, episodio_aberto) ou (None, False). Leitura na sessão
    principal (sem escrita); erro = (None, False)."""
    try:
        row = db.session.get(PresencaHumanaConversa, str(conv_id))
        if row is None:
            return None, False
        return row.nota_em, bool(row.notas)
    except Exception:  # noqa: BLE001
        logger.exception('presenca_humana: leitura falhou conv=%s', conv_id)
        return None, False


def humano_presente(conv_id, *, horas=PRESENCA_HUMANA_HORAS,
                    consultar_chatwoot=False):
    """True se um humano escreveu nota privada nesta conversa nas últimas
    `horas` e o episódio não foi encerrado. Com `consultar_chatwoot=True`,
    relê a API do Chatwoot como rede de segurança: nota MAIS NOVA que a
    conhecida (ou sem marcador) dentro da janela reabre e persiste; se o
    banco falhar com a nota na mão, é presença mesmo assim. Falha de
    LEITURA = False (fail-open para a fala automática: o que barra é o
    SINAL, nunca a ausência de sinal por erro)."""
    if not conv_id:
        return False
    corte = agora() - timedelta(hours=horas)
    nota_em, aberto = estado(conv_id)
    if aberto and nota_em is not None and nota_em >= corte:
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
    res = registrar_nota_privada(conv_id, na_api.get('autor'),
                                 quando=na_api.get('quando'))
    return True if res is None else res

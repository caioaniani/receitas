"""Encaminhamento durável: a espera humana existe antes de chamar o Chatwoot."""
import logging

from app.extensions import db
from app.models import AppConfig, EsperaAtendimento
from app.utils import agora

POLITICA_ATENDIMENTO = 'restrito_2026_09_24'
ESTADOS_PENDENTES = ('aguardando', 'em_atendimento', 'respondido')
CHAVE_CURSOR = 'atendimento_restrito_recuperacao_cursor'
logger = logging.getLogger(__name__)


def encaminhamento_pendente(conv_id):
    """Sem vencimento: somente uma resolução humana encerra a espera."""
    if not conv_id:
        return False
    row = (EsperaAtendimento.query.populate_existing()
           .filter_by(conversa_id=str(conv_id)).first())
    if row:
        return row.estado in ESTADOS_PENDENTES
    # Conversas transferidas antes desta política também ficam com a equipe.
    from app.services import chatbot
    return any(m.get('handoff_em') and not m.get('herdada')
               for m in (chatbot.carregar_historico(conv_id) or []))


def registrar_encaminhamento(conv_id, historico, *, nome=''):
    """Persiste a fila, sem inventar alerta grave. Falha impede fala automática.

    Retorna True apenas quando começa um episódio novo. O relógio e a
    confirmação inicial não se renovam a cada mensagem do cliente.
    """
    if not conv_id:
        raise ValueError('encaminhamento sem conversa')
    cid = str(conv_id)
    row = (EsperaAtendimento.query.populate_existing()
           .filter_by(conversa_id=cid).first())
    novo = row is None or row.estado not in ESTADOS_PENDENTES
    momento = agora()
    if row is None:
        row = EsperaAtendimento(conversa_id=cid, inicio_em=momento)
        db.session.add(row)
    if novo:
        row.inicio_em = momento
        row.grave = False
        row.resolvido_em = None
        row.estado = 'aguardando'
    if nome:
        row.nome = nome[:200]
    mensagens = [m.get('content') or '[Anexo enviado pelo cliente]'
                 for m in historico or [] if m.get('role') == 'user']
    if mensagens:
        row.mensagem = '\n'.join(mensagens[-3:])[-2000:]
    # Também torna a espera visível imediatamente no painel e no monitor.
    if row.proximo_aviso_em is None:
        row.proximo_aviso_em = momento
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return novo


def recuperar_encaminhamentos_pendentes(limite=40):
    """Recupera a fila após falha do Chatwoot, sem nenhuma fala ao cliente."""
    from app.blueprints.crm.routes import _lock_conv_cross_worker, _lock_para_conv
    from app.services import chatwoot
    cursor = AppConfig.get(CHAVE_CURSOR) or ''
    consulta = EsperaAtendimento.query.filter(
        EsperaAtendimento.estado.in_(ESTADOS_PENDENTES))
    linhas = (consulta.filter(EsperaAtendimento.conversa_id > cursor)
              .order_by(EsperaAtendimento.conversa_id).limit(limite).all())
    if not linhas:
        linhas = consulta.order_by(EsperaAtendimento.conversa_id).limit(limite).all()
    resumo = {'consultadas': 0, 'abertas': 0, 'resolvidas': 0}
    ids = [r.conversa_id for r in linhas]
    for cid in ids:
        try:
            with _lock_para_conv(cid), _lock_conv_cross_worker(cid):
                row = (EsperaAtendimento.query.populate_existing()
                       .filter_by(conversa_id=cid).first())
                if not row or row.estado not in ESTADOS_PENDENTES:
                    continue
                atual = chatwoot.consultar_conversa(cid)
                resumo['consultadas'] += 1
                if not atual:
                    continue
                if atual.get('status') == 'resolved':
                    row.estado = 'resolvido'
                    row.resolvido_em = agora()
                    row.proximo_aviso_em = None
                    db.session.commit()
                    resumo['resolvidas'] += 1
                elif atual.get('status') == 'pending':
                    res = chatwoot.definir_status(cid, 'open')
                    resumo['abertas'] += int(bool(res.get('ok')))
        except Exception:  # noqa: BLE001
            db.session.rollback()
            logger.exception('atendimento: recuperar fila falhou conv=%s', cid)
        finally:
            # Rodízio independente do relógio de alertas: casos antigos já
            # abertos não impedem recuperar os seguintes na próxima rodada.
            AppConfig.set(CHAVE_CURSOR, cid)
            db.session.commit()
    return resumo

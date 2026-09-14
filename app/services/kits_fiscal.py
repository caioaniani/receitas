"""Fila fiscal durável por entrega de kit, independente do webhook de pagamento."""
import logging
from datetime import timedelta

from sqlalchemy import or_

from app.extensions import db
from app.models import EntregaKit, PedidoOnline, TarefaFiscalKit
from app.services import email as email_svc
from app.services import tiny_nf
from app.utils import agora

logger = logging.getLogger(__name__)


def processar_pendentes(limite=5, *, base=None):
    """Emite até cinco notas devidas e conserva falhas para nova tentativa.

    Chamado pelo cron sob o lock global 7765. A emissão em si usa também
    a trava por pedido de tiny_nf.emitir_nf, compartilhada com o botão do
    admin e mantida durante os commits internos do Tiny.
    """
    base = base or agora()
    limite = max(1, min(int(limite), 5))
    ids = [row.pedido_id for row in TarefaFiscalKit.query.filter(
        TarefaFiscalKit.concluido_em.is_(None),
        or_(TarefaFiscalKit.proxima_tentativa_em.is_(None),
            TarefaFiscalKit.proxima_tentativa_em <= base),
    ).order_by(TarefaFiscalKit.proxima_tentativa_em, TarefaFiscalKit.criado_em,
               TarefaFiscalKit.pedido_id).limit(limite).all()]
    resumo = {'processados': 0, 'concluidos': 0, 'pendentes': 0, 'ignorados': 0}
    for pedido_id in ids:
        tarefa = db.session.get(TarefaFiscalKit, pedido_id)
        if not tarefa or tarefa.concluido_em:
            continue
        pedido = db.session.get(PedidoOnline, pedido_id)
        resumo['processados'] += 1
        if not pedido or not db.session.get(EntregaKit, pedido_id) or pedido.status == 'cancelado':
            tarefa.concluido_em = base
            tarefa.erro = 'Emissão dispensada: entrega cancelada ou sem vínculo com kit.'
            db.session.commit()
            resumo['ignorados'] += 1
            continue
        # Um worker interrompido não faz a tarefa desaparecer: o próximo
        # ciclo retoma após esta janela e reutiliza o id da NF já persistido.
        tarefa.tentativas = (tarefa.tentativas or 0) + 1
        tarefa.proxima_tentativa_em = base + timedelta(minutes=5)
        db.session.commit()
        try:
            resultado = ({'ok': True} if pedido.nf_emitida_em and pedido.tiny_nota_fiscal_id
                         else tiny_nf.emitir_nf(pedido))
        except Exception:  # noqa: BLE001 — falha fica registrada para retry durável
            db.session.rollback()
            logger.exception('Fila fiscal de kits: falha no pedido %s', pedido_id)
            resultado = {'ok': False, 'msg': 'Falha ao emitir a NF. O sistema tentará novamente.'}
        # Tiny faz commits próprios. A releitura também cobre uma NF que o
        # admin tenha concluído enquanto esta tarefa aguardava sua trava.
        tarefa = db.session.get(TarefaFiscalKit, pedido_id)
        pedido = db.session.get(PedidoOnline, pedido_id)
        db.session.refresh(pedido)
        if pedido.nf_emitida_em and pedido.tiny_nota_fiscal_id:
            if not tarefa.email_enviado_em:
                try:
                    envio = (email_svc.enviar_nf_emitida(pedido) if email_svc.disponivel()
                             else {'ok': False, 'erro': 'Serviço de e-mail indisponível.'})
                except Exception:  # noqa: BLE001 — retry somente do e-mail, preservando a NF emitida
                    logger.exception('Fila fiscal de kits: e-mail falhou no pedido %s', pedido_id)
                    envio = {'ok': False, 'erro': 'Falha ao enviar o e-mail.'}
                if envio and envio.get('ok'):
                    tarefa.email_enviado_em = base
                else:
                    resultado = {'ok': False, 'msg': 'NF emitida; envio do DANFE por e-mail '
                                 'pendente: ' + str((envio or {}).get('erro') or 'Tente novamente.')}
            if tarefa.email_enviado_em:
                tarefa.concluido_em = base
                tarefa.erro = None
                resumo['concluidos'] += 1
        elif pedido.status == 'cancelado':
            tarefa.concluido_em = base
            tarefa.erro = 'Emissão dispensada: entrega cancelada.'
            resumo['ignorados'] += 1
        if not tarefa.concluido_em:
            tarefa.erro = str(resultado.get('msg') or 'A emissão ainda não foi confirmada.')[:2000]
            atraso = min(60, 5 * (2 ** min(max(tarefa.tentativas - 1, 0), 4)))
            tarefa.proxima_tentativa_em = base + timedelta(minutes=atraso)
            resumo['pendentes'] += 1
        db.session.commit()
    return resumo

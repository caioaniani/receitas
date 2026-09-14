"""NF do site uma hora antes da entrega, seguida de e-mail com o DANFE."""
import logging
import re
from datetime import datetime, time, timedelta

from sqlalchemy import or_

from app.extensions import db
from app.models import PedidoOnline, TarefaFiscalPedido
from app.services import email as email_svc
from app.utils import BRT, agora

logger = logging.getLogger(__name__)
STATUS_PAGOS = ('pago', 'em_preparo', 'a_caminho', 'entregue')


def _brt(instante):
    return instante.astimezone(BRT).replace(tzinfo=None) if instante.tzinfo else instante


def horario_emissao(pedido):
    """Uma hora antes do INÍCIO da janela, em BRT; None se ilegível.

    Express não tem horário fixo e pode sair logo: emite após o pagamento.
    Nunca inventa horário para agendamento incompleto.
    """
    janela = (pedido.janela_entrega or '').strip().lower()
    if pedido.modo_entrega == 'express':
        return _brt(pedido.pago_em) if pedido.pago_em else None
    if not pedido.data_entrega:
        return None
    partes = re.split(r'\s*(?:[-–—]|\s+às?\s+|\s+as?\s+)\s*', janela)
    if not 1 <= len(partes) <= 2:
        return None
    horarios = []
    for parte in partes:
        match = re.fullmatch(r'(\d{1,2})(?::([0-5]\d)|h([0-5]\d)?)', parte.strip())
        if not match or int(match[1]) > 23:
            return None
        horarios.append(time(int(match[1]), int(match[2] or match[3] or 0)))
    if len(horarios) == 2 and horarios[1] <= horarios[0]:
        return None
    return datetime.combine(pedido.data_entrega, horarios[0]) - timedelta(hours=1)


def pode_emitir(pedido):
    return bool(pedido.pago_em and pedido.status in STATUS_PAGOS and not pedido.divulgacao)


def agendar(pedido, *, base=None):
    """Grava junto ao pagamento, SEM commit nem rede. Repetição preserva envio."""
    db.session.flush()
    db.session.refresh(pedido, with_for_update=True)
    tarefa = db.session.get(TarefaFiscalPedido, pedido.id)
    if tarefa is None:
        tarefa = TarefaFiscalPedido(
            pedido_id=pedido.id,
            proxima_tentativa_em=horario_emissao(pedido) or _brt(base or agora()))
        db.session.add(tarefa)
    return tarefa


def recuperar_sem_tarefa():
    """Recupera pedidos pagos em andamento, inclusive anteriores ao deploy.

    Não refatura o histórico já entregue nem reenvia notas autorizadas pelo
    fluxo antigo. Tarefas existentes continuam até terminar emissão/e-mail.
    """
    pedidos = PedidoOnline.query.outerjoin(
        TarefaFiscalPedido, TarefaFiscalPedido.pedido_id == PedidoOnline.id,
    ).filter(
        TarefaFiscalPedido.pedido_id.is_(None),
        PedidoOnline.pago_em.isnot(None),
        PedidoOnline.status.in_(STATUS_PAGOS[:-1]),
        PedidoOnline.divulgacao.is_(False),
        PedidoOnline.nf_emitida_em.is_(None),
    ).order_by(PedidoOnline.id).all()
    for pedido in pedidos:
        db.session.refresh(pedido, with_for_update=True)
        if pode_emitir(pedido) and not pedido.nf_emitida_em:
            agendar(pedido)
    db.session.commit()


def reagendar(pedido):
    """Edição logística atualiza o prazo sem reabrir NF/e-mail já concluídos."""
    tarefa = db.session.get(TarefaFiscalPedido, pedido.id)
    if tarefa and not tarefa.concluido_em and not pedido.nf_emitida_em:
        tarefa.proxima_tentativa_em = horario_emissao(pedido) or agora()


def enviar_danfe(pedido, *, reenviar=False, base=None):
    """Envio manual/automático compartilha trava e comprovante persistente."""
    from app.services import tiny_nf

    base = _brt(base or agora())
    with tiny_nf._trava_nf_kit(pedido.id) as adquirido:
        if not adquirido:
            return {'ok': False, 'erro': 'A NF deste pedido já está sendo processada.'}
        db.session.refresh(pedido)
        if not pedido.nf_emitida_em or not pedido.tiny_nota_fiscal_id:
            return {'ok': False, 'erro': 'A NF ainda não foi autorizada.'}
        tarefa = agendar(pedido, base=base)
        db.session.flush()
        db.session.refresh(tarefa)
        if tarefa.email_enviado_em and not reenviar:
            return {'ok': True}
        resultado = (email_svc.enviar_nf_emitida(pedido) if email_svc.disponivel()
                     else {'ok': False, 'erro': 'Serviço de e-mail indisponível.'})
        if resultado and resultado.get('ok'):
            tarefa.email_enviado_em = base
            tarefa.concluido_em = base
            tarefa.erro = None
            db.session.commit()
        return resultado


def processar_pendentes(limite=25, *, base=None):
    """Cron a cada minuto sob lock global 7765; Tiny trava cada pedido.

    Só aplica o limite depois de conferir o horário: tarefas futuras não
    ocupam o lote de quem já precisa emitir. Agenda é relida em cada ciclo.
    """
    from app.services import tiny_nf

    base = _brt(base or agora())
    limite = max(1, min(int(limite), 25))
    recuperar_sem_tarefa()
    tarefas = TarefaFiscalPedido.query.outerjoin(
        PedidoOnline, PedidoOnline.id == TarefaFiscalPedido.pedido_id,
    ).filter(
        TarefaFiscalPedido.concluido_em.is_(None),
        or_(TarefaFiscalPedido.tentativas == 0,
            TarefaFiscalPedido.proxima_tentativa_em <= base),
        or_(PedidoOnline.id.is_(None), PedidoOnline.status == 'cancelado',
            PedidoOnline.nf_emitida_em.isnot(None), PedidoOnline.data_entrega.is_(None),
            PedidoOnline.data_entrega <= (base + timedelta(hours=1)).date()),
    ).order_by(PedidoOnline.data_entrega, PedidoOnline.janela_entrega,
               TarefaFiscalPedido.pedido_id).all()
    resumo = {'processados': 0, 'concluidos': 0, 'pendentes': 0, 'ignorados': 0}
    for tarefa in tarefas:
        if resumo['processados'] >= limite:
            break
        pedido_id = tarefa.pedido_id
        pedido = db.session.get(PedidoOnline, pedido_id)
        if not pedido or not pode_emitir(pedido):
            tarefa.concluido_em = base
            tarefa.erro = 'Emissão dispensada: pedido cancelado ou sem pagamento confirmado.'
            db.session.commit()
            resumo['processados'] += 1
            resumo['ignorados'] += 1
            continue
        horario = horario_emissao(pedido)
        if not pedido.nf_emitida_em:
            if horario is None:
                tarefa.erro = 'Informe uma data e um horário de entrega válidos para agendar a NF.'
                db.session.commit()
                continue
            if horario > base:
                if not tarefa.tentativas:
                    tarefa.proxima_tentativa_em = horario
                    db.session.commit()
                continue
        resumo['processados'] += 1
        tarefa.tentativas = (tarefa.tentativas or 0) + 1
        tarefa.proxima_tentativa_em = base + timedelta(minutes=5)
        db.session.commit()
        try:
            resultado = ({'ok': True} if pedido.nf_emitida_em and pedido.tiny_nota_fiscal_id
                         else tiny_nf.emitir_nf(pedido, automatico=True, base=base))
        except Exception:  # noqa: BLE001 — conserva a tarefa após falha externa
            db.session.rollback()
            logger.exception('Fila fiscal: falha no pedido %s', pedido_id)
            resultado = {'ok': False, 'msg': 'Falha ao emitir a NF. Confira a pendência fiscal.'}
        tarefa = db.session.get(TarefaFiscalPedido, pedido_id)
        pedido = db.session.get(PedidoOnline, pedido_id)
        db.session.refresh(pedido)
        if not pode_emitir(pedido):
            tarefa.concluido_em = base
            tarefa.erro = 'Pedido cancelado durante o processamento; confira eventual NF no Tiny.'
            resumo['ignorados'] += 1
        elif pedido.nf_emitida_em and pedido.tiny_nota_fiscal_id:
            if not tarefa.email_enviado_em:
                try:
                    envio = enviar_danfe(pedido, base=base)
                except Exception:  # noqa: BLE001 — retry só do e-mail, sem outra NF
                    logger.exception('Fila fiscal: e-mail falhou no pedido %s', pedido_id)
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
        if not tarefa.concluido_em:
            tarefa.erro = str(resultado.get('msg') or 'A emissão ainda não foi confirmada.')[:2000]
            atraso = min(60, 5 * (2 ** min(max(tarefa.tentativas - 1, 0), 4)))
            tarefa.proxima_tentativa_em = base + timedelta(minutes=atraso)
            resumo['pendentes'] += 1
        db.session.commit()
    return resumo

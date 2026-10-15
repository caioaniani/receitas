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


# ── NF do site PARADA: o dono precisa saber (15/10/2026) ─────────────────────
# Caso ca37c72e: compra com CNPJ em 28/09, dados fiscais DECLARADOS pelo
# cliente (a consulta pública não confirmou a inscrição estadual). A emissão
# depende da conferência do dono (`fiscal_online.salvar_conferencia`), e a
# fila só repetia a tentativa de hora em hora: 17 dias sem nota, ninguém
# avisado, cliente ameaçando denunciar. A conferência continua sendo gesto do
# dono (define a situação de IE na nota); o que muda é que a fila PARADA vira
# aviso — WhatsApp uma vez por dia e item no "Precisa de você hoje".

TENTATIVAS_PARA_AVISO = 2
AVISO_HORA_INICIO, AVISO_HORA_FIM = 8, 21
_CHAVE_AVISO = 'nf_site_travadas_aviso'


def travadas(base=None):
    """Tarefas de NF do site que já tentaram e não saíram, com o motivo.

    Só pedido que ainda deve ter nota (pago, não divulgação, não cancelado).
    Cada item: {codigo, pedido_id, data_entrega, tentativas, erro,
    aguarda_dono, desde}. `aguarda_dono` = trava do cadastro fiscal declarado
    que só a conferência do dono libera.
    """
    from app.models import FiscalPedidoOnline
    base = _brt(base or agora())
    linhas = (db.session.query(TarefaFiscalPedido, PedidoOnline, FiscalPedidoOnline)
              .join(PedidoOnline, PedidoOnline.id == TarefaFiscalPedido.pedido_id)
              .outerjoin(FiscalPedidoOnline,
                         FiscalPedidoOnline.pedido_id == TarefaFiscalPedido.pedido_id)
              .filter(TarefaFiscalPedido.concluido_em.is_(None),
                      TarefaFiscalPedido.tentativas >= TENTATIVAS_PARA_AVISO,
                      TarefaFiscalPedido.erro.isnot(None))
              .order_by(PedidoOnline.data_entrega, PedidoOnline.id).all())
    out = []
    for tarefa, pedido, fiscal in linhas:
        if not pode_emitir(pedido):
            continue
        aguarda_dono = bool(fiscal is not None and fiscal.erro and not fiscal.confirmado_em
                            and fiscal.origem == 'checkout_declarado')
        out.append({'codigo': pedido.codigo, 'pedido_id': pedido.id,
                    'data_entrega': pedido.data_entrega, 'tentativas': tarefa.tentativas,
                    'erro': (tarefa.erro or '')[:300], 'aguarda_dono': aguarda_dono,
                    'nf_emitida': bool(pedido.nf_emitida_em),
                    'desde': horario_emissao(pedido) or tarefa.criado_em})
    return out


def _texto_aviso(itens):
    from flask import current_app
    base_url = (current_app.config.get('APP_BASE_URL') or '').rstrip('/')
    linhas = [f'🧾 NF do site PARADA — {len(itens)} pedido(s) sem nota fiscal:']
    for it in itens[:10]:
        entrega = it['data_entrega'].strftime('%d/%m') if it['data_entrega'] else '?'
        if it['aguarda_dono']:
            motivo = 'falta você conferir o cadastro fiscal (CNPJ/IE) no pedido'
        elif it['nf_emitida'] and 'inactive' in it['erro'].lower():
            motivo = ('NF emitida, mas o e-mail do cliente foi recusado (endereço '
                      'inativo ou digitado errado) — corrija o e-mail no pedido')
        elif it['nf_emitida']:
            motivo = 'NF emitida, envio do DANFE por e-mail pendente'
        else:
            motivo = it['erro'][:140]
        linhas.append(f'• {it["codigo"]} (entrega {entrega}) — {motivo}')
        if base_url:
            linhas.append(f'  {base_url}/admin/loja-online/pedidos/{it["codigo"]}')
    if len(itens) > 10:
        linhas.append(f'… e mais {len(itens) - 10}.')
    linhas.append('Depois da conferência a nota sai sozinha em ~1 min e vai por e-mail ao cliente.')
    return '\n'.join(linhas)


def avisar_travadas(base=None):
    """WhatsApp ao dono com as NFs paradas: no máximo UMA vez por dia
    (claim persistente — dois workers/containers não duplicam), entre 8h e
    21h. A lista nova do mesmo dia não reenvia; a cobrança volta no dia
    seguinte enquanto houver nota parada. Nunca levanta."""
    from flask import current_app

    from app.services import whatsapp, zapi
    try:
        base = _brt(base or agora())
        if str(current_app.config.get('NF_SITE_AVISO', '1')).strip() == '0':
            return {'enviado': False, 'motivo': 'desligado'}
        if not AVISO_HORA_INICIO <= base.hour < AVISO_HORA_FIM:
            return {'enviado': False, 'motivo': 'fora_do_horario'}
        itens = travadas(base)
        if not itens:
            return {'enviado': False, 'motivo': 'nada_parado'}
        numero = ((current_app.config.get('LOJA_ALERTA_NUMERO') or '').strip()
                  or (current_app.config.get('ZAPI_NUMERO_DESTINO') or '').strip())
        if not numero:
            return {'enviado': False, 'motivo': 'sem_numero'}
        estado, anterior = whatsapp.claim_envio(_CHAVE_AVISO, base.date().isoformat())
        if estado != 'ok':
            return {'enviado': False, 'motivo': estado}
        res = zapi.enviar_texto(numero, _texto_aviso(itens))
        if not (res or {}).get('ok'):
            whatsapp.devolver_claim(_CHAVE_AVISO, anterior)
            return {'enviado': False, 'motivo': 'falha_envio', 'n': len(itens)}
        return {'enviado': True, 'n': len(itens)}
    except Exception:  # noqa: BLE001 — aviso nunca derruba a fila fiscal
        logger.exception('Fila fiscal: aviso de NF parada falhou')
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {'enviado': False, 'motivo': 'erro'}

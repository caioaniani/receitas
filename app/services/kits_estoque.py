"""Estoque na coleta de cada kit e expiração do pagamento do ciclo.

A coleta não commita: o caller confirma a saída na mesma transação. A
expiração consulta o gateway e commita por compra (`expirar_compras`).
"""
import logging
from datetime import timedelta

from app.extensions import db
from app.models import CompraKit, EntregaKit, PedidoOnline
from app.utils import agora

logger = logging.getLogger(__name__)


def ja_coletado(pedido_id):
    """Marcador físico de uma entrega, independente do pagamento do mês."""
    return (db.session.query(EntregaKit.pedido_id)
            .filter(EntregaKit.pedido_id == pedido_id,
                    EntregaKit.coletado_em.isnot(None)).first() is not None)


def registrar_coleta(pedido, *, usuario_id=None, acertado=False):
    """Baixa itens comuns uma vez, com o pedido já travado pelo caller.

    Chamado por saida_producao_site após global do acerto -> pedido. Não
    adquire a trava da compra: pagamento/expiração usam compra -> pedidos.
    Sob encomenda continua no motor de saída da indústria do caller.
    """
    entrega = (EntregaKit.query.filter_by(pedido_id=pedido.id)
               .populate_existing().first())
    if (entrega is None or pedido.divulgacao
            or pedido.status not in ('pago', 'em_preparo', 'a_caminho')):
        return None
    from app.models.kits_cafe import ReembolsoKit
    reembolso = (ReembolsoKit.query.filter_by(pedido_id=pedido.id)
                 .populate_existing().first())
    if reembolso and reembolso.status in ('solicitado', 'confirmado'):
        raise ValueError(f'Pedido {pedido.codigo}: há um reembolso em andamento ou confirmado; '
                         'a coleta deste kit está bloqueada.')
    if entrega.coletado_em is not None:
        return None

    resultado = {'baixado': 0, 'faltou': 0, 'pulado': 0}
    if not acertado:
        from app.services.loja_pagamento import _baixar_estoque
        resultado = _baixar_estoque(pedido, usuario_id=usuario_id)
    # Acerto anterior já despachou da indústria. Falta de saldo também é
    # saída concluída: o motor registra a divergência e uma reposição futura
    # não pode causar débito tardio em webhook repetido.
    entrega.coletado_em = agora()
    db.session.flush()
    return resultado


# Compra cujo pagamento o gateway ainda não deixa encerrar (QR do Pix válido,
# cartão em análise, Pagar.me fora) volta pro fim da fila, como o avulso
# (loja_estoque_reserva._ADIAR_EXPIRACAO): sem isso as presas ocupariam
# sempre o começo do lote e nenhuma compra nova expiraria.
_ADIAR_EXPIRACAO = timedelta(minutes=5)
# Compra esperando o gateway há mais que isso gera UM aviso por dia ao dono.
_PRESO_ALERTA = timedelta(hours=24)


def expirar_compras(*, base=None, max_lote=200):
    """Cancela ciclos vencidos ainda não pagos, sem tocar estoque físico.

    Cada compra é resolvida numa transação própria, em DUAS fases (mesmo
    desenho do avulso, `loja_estoque_reserva._expirar_um`):
    1. sem nenhuma trava, consulta no Pagar.me as tentativas pendentes do
       pedido principal — o webhook pode ter falhado com o dinheiro recebido,
       ou o QR do Pix ainda está pagável;
    2. trava compra → pedidos por id (mesma ordem do pagamento), reconfere e
       cancela, marca pago ou adia — com commit ao final.
    Rede nunca roda segurando FOR UPDATE nem trava de capacidade. Uma compra
    com erro não derruba o lote. Devolve os códigos das entregas canceladas.
    """
    base = base or agora()
    ids = [cid for (cid,) in (
        db.session.query(CompraKit.id)
        .join(EntregaKit, EntregaKit.compra_id == CompraKit.id)
        .join(PedidoOnline, PedidoOnline.id == EntregaKit.pedido_id)
        .filter(CompraKit.pago_em.is_(None), CompraKit.expira_em < base,
                PedidoOnline.status == 'aguardando_pagamento')
        .group_by(CompraKit.id, CompraKit.expira_em)
        .order_by(CompraKit.expira_em, CompraKit.id)
        .limit(max_lote).all())]
    db.session.commit()
    codigos = []
    for compra_id in ids:
        try:
            codigos.extend(_expirar_compra(compra_id, base))
        except Exception:  # noqa: BLE001 — uma compra não trava o lote
            db.session.rollback()
            logger.exception('expirar_compras: compra de kit %s falhou', compra_id)
    if codigos:
        logger.info('expirar_compras: %d entrega(s) de kits cancelada(s): %s',
                    len(codigos), ', '.join(codigos))
    return codigos


def _expirar_compra(compra_id, base):
    from app.services import loja_pagamento
    from app.services.compra_kits import reler_entregas
    from app.services.kits_capacidade import liberar_entregas

    # Fase 1 — sem travas: o que o gateway diz das tentativas pendentes.
    compra = db.session.get(CompraKit, compra_id)
    if compra is None or compra.pago_em is not None or compra.expira_em >= base:
        db.session.commit()
        return []
    principal = compra.pedido_principal
    situacao, pagamento = loja_pagamento.situacao_no_gateway(principal, base=base)
    db.session.commit()   # grava tentativas encerradas lá; solta tudo

    # Fase 2 — confirmação (gateway ou owner) pode ter chegado após a consulta.
    with db.session.no_autoflush:
        db.session.refresh(compra, with_for_update=True)
        if compra.pago_em is not None or compra.expira_em >= base:
            db.session.commit()
            return []
        pedidos = (PedidoOnline.query
                   .join(EntregaKit, EntregaKit.pedido_id == PedidoOnline.id)
                   .filter(EntregaKit.compra_id == compra.id)
                   .order_by(PedidoOnline.id).with_for_update(of=PedidoOnline)
                   .populate_existing().all())
    if not pedidos or any(p.pago_em is not None or p.status != 'aguardando_pagamento'
                          for p in pedidos):
        db.session.commit()
        return []
    if situacao == 'pago':
        mudou = loja_pagamento._marcar_pago(principal, pagamento, enviar_confirmacao=False)
        db.session.commit()
        logger.warning('expirar_compras: compra de kit %s (%s) estava PAGA no gateway sem '
                       'webhook — marcada paga em vez de cancelada', compra.id, principal.codigo)
        if mudou:
            from app.services.kits_pagamento import apos_confirmacao
            apos_confirmacao(principal)
        return []
    if situacao != 'livre':
        # QR ainda pagável, cartão em análise ou gateway fora: cancelar agora
        # deixaria o cliente pagar por uma compra morta. Adia só este ciclo.
        compra.expira_em = base + _ADIAR_EXPIRACAO
        db.session.commit()
        if compra.criado_em and base - compra.criado_em > _PRESO_ALERTA:
            from app.services import loja_alerta
            loja_alerta.alertar_pedido_pago(
                f'⚠️ Compra de kit {principal.codigo} ({compra.kit_nome}) está há mais de '
                '24 h esperando o Pagar.me confirmar o pagamento (cartão em análise ou '
                'gateway sem resposta). A capacidade das datas continua reservada — '
                'confira no painel do Pagar.me.',
                f'kit_preso_gateway|{compra.id}', critico=False, cooldown=86400)
        return []
    liberar_entregas(reler_entregas(compra))
    codigos = []
    for pedido in pedidos:
        pedido.status = 'cancelado'
        pedido.motivo_cancelamento = 'pix_expirado'
        pedido.cancelado_em = base
        codigos.append(pedido.codigo)
    db.session.commit()
    return codigos

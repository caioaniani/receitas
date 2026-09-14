"""Estoque na coleta de cada kit e expiração do pagamento do ciclo.

Não commita. O caller confirma a saída ou a expiração na mesma transação.
"""
import logging

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


def expirar_compras(*, base=None, max_lote=200):
    """Cancela ciclos vencidos ainda não pagos, sem tocar estoque físico.

    Serializa com o pagamento: compra primeiro, depois pedidos por id.
    A consulta inicial só escolhe candidatos; as condições são relidas
    depois da espera pelas travas. Não commita.
    """
    base = base or agora()
    candidatos = (CompraKit.query.join(EntregaKit, EntregaKit.compra_id == CompraKit.id)
                  .join(PedidoOnline, PedidoOnline.id == EntregaKit.pedido_id)
                  .filter(CompraKit.pago_em.is_(None), CompraKit.expira_em < base,
                          PedidoOnline.status == 'aguardando_pagamento')
                  .distinct().order_by(CompraKit.expira_em, CompraKit.id)
                  .limit(max_lote).all())
    codigos = []
    entregas_a_liberar = []
    pedidos_a_cancelar = []
    # Todos os grupos/pedidos antes de qualquer plano. Em seguida devolve o
    # lote por data/kind/id: não segura plano de uma compra enquanto aguarda
    # outra compra cujo pagamento esteja usando os mesmos itens/datas.
    for compra in sorted(candidatos, key=lambda c: c.id):
        with db.session.no_autoflush:
            db.session.refresh(compra, with_for_update=True)
            if compra.pago_em is not None or compra.expira_em >= base:
                continue
            pedidos = (PedidoOnline.query
                       .join(EntregaKit, EntregaKit.pedido_id == PedidoOnline.id)
                       .filter(EntregaKit.compra_id == compra.id)
                       .order_by(PedidoOnline.id).with_for_update(of=PedidoOnline)
                       .populate_existing().all())
        if not pedidos or any(p.pago_em is not None or p.status != 'aguardando_pagamento'
                              for p in pedidos):
            continue
        from app.services.compra_kits import reler_entregas
        entregas_a_liberar.extend(reler_entregas(compra))
        pedidos_a_cancelar.extend(pedidos)
    from app.services.kits_capacidade import liberar_entregas
    liberar_entregas(entregas_a_liberar)
    for pedido in pedidos_a_cancelar:
        pedido.status = 'cancelado'
        pedido.motivo_cancelamento = 'pix_expirado'
        pedido.cancelado_em = base
        codigos.append(pedido.codigo)
    if codigos:
        db.session.flush()
        logger.info('expirar_compras: %d entrega(s) de kits cancelada(s): %s',
                    len(codigos), ', '.join(codigos))
    return codigos

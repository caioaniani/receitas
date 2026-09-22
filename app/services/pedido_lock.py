"""Pedidos e estoque da mesma loja compartilham uma trava transacional.

O motor e os gestos humanos devem travar ANTES de reler/adotar/alterar o
pedido. A trava também cobre um dia sem pedido, quando ainda não há linha
para FOR UPDATE. Usar a mesma ordem de locks do estoque evita inversões
em recebimento, cancelamento e movimentos entre lojas.
"""
from app.constants import STATUS_PEDIDO_EDITAVEIS, STATUS_PEDIDO_ENTREGUES
from app.extensions import db
from app.services.estoque_helpers import serializar_lojas
from app.utils import hoje


def travar_pedidos_lojas(loja_ids):
    serializar_lojas(loja_ids)


def reler_pedido_travado(pedido):
    """Recarrega sob a trava, descartando só o snapshot anterior à edição."""
    travar_pedidos_lojas([pedido.loja_id])
    db.session.refresh(pedido)
    db.session.expire(pedido, ['itens'])
    return pedido


def protegido_do_motor(pedido, hoje_d=None):
    """Cancelamento humano também protege o dia contra recriação."""
    if pedido.status == 'cancelado':
        return pedido.modificado_por_id is not None
    if (pedido.status in STATUS_PEDIDO_ENTREGUES
            and pedido.data_entrega and pedido.data_entrega > (hoje_d or hoje())):
        return False
    return (pedido.criado_por is not None
            or pedido.modificado_por_id is not None
            or pedido.status not in STATUS_PEDIDO_EDITAVEIS)

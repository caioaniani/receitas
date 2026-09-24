"""Helper compartilhado de geracao do QR de saida (handshake).

Centraliza a criacao do PedidoQRCode tipo 'saida' usada tanto pela rota
classica (`pedidos.qr_saida`) quanto pela tela touchscreen do padeiro.
NAO valida status nem motorista — quem chama faz isso.
"""
import secrets
from datetime import timedelta

from app.extensions import db
from app.models import PedidoQRCode
from app.utils import agora


def gerar_qr_saida(pedido, criado_por_id):
    """Reusa o QR de saida ativo do pedido ou cria um novo (TTL 2h).
    Retorna o PedidoQRCode."""
    qr = (PedidoQRCode.query
          .filter_by(pedido_id=pedido.id, tipo='saida', usado_em=None)
          .filter(PedidoQRCode.expira_em > agora())
          .order_by(PedidoQRCode.criado_em.desc())
          .first())
    if not qr:
        qr = PedidoQRCode(
            token=secrets.token_urlsafe(24),
            pedido_id=pedido.id,
            tipo='saida',
            criado_por_id=criado_por_id,
            expira_em=agora() + timedelta(hours=2),
        )
        db.session.add(qr)
        db.session.commit()
    return qr


def gerar_qr_retirada(retirada, tipo, criado_por_id=None, ttl_horas=48):
    """Reusa o QR ativo da retirada (por tipo) ou cria um novo.

    TTL longo (48h, vs 2h do pedido): a retirada e criada na VESPERA pelo bot
    e coletada no dia seguinte — o QR precisa sobreviver a noite. Single-use +
    guarda de status seguram o resto. NAO commita."""
    from app.models import RetiradaQRCode
    qr = (RetiradaQRCode.query
          .filter_by(retirada_id=retirada.id, tipo=tipo, usado_em=None)
          .filter(RetiradaQRCode.expira_em > agora())
          .order_by(RetiradaQRCode.criado_em.desc())
          .first())
    if not qr:
        qr = RetiradaQRCode(
            token=secrets.token_urlsafe(24),
            retirada_id=retirada.id,
            tipo=tipo,
            criado_por_id=criado_por_id,
            expira_em=agora() + timedelta(hours=ttl_horas),
        )
        db.session.add(qr)
        db.session.flush()
    return qr

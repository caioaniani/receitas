"""Smoke test: mudar_status pra separado gera PedidoQRCode + qr_url no resultado."""
from datetime import date, timedelta


def test_mudar_status_separar_gera_qr(app, admin_user, loja, catalogo):
    """Status confirmado → separar deve criar PedidoQRCode tipo='saida' +
    incluir qr_url/qr_png_url no resultado pro Slack mostrar."""
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja, PedidoQRCode
    from app.services import copilot

    p = PedidoLoja(loja_id=loja.id, status='confirmado',
                   data_entrega=date.today() + timedelta(days=1))
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id,
                               receita_id=catalogo['receita'].id,
                               quantidade=2))
    db.session.commit()

    with app.test_request_context():  # url_for precisa de request context
        out = copilot.executar_mudar_status_pedido(
            {'pedido_id': p.id, 'novo_status': 'separar'},
            admin_user,
        )
    assert out['ok'] is True
    assert out['novo_status'] == 'separado'
    db.session.refresh(p)
    assert p.status == 'separado'
    qrs = PedidoQRCode.query.filter_by(pedido_id=p.id, tipo='saida').all()
    assert len(qrs) == 1
    assert out.get('qr_url') is not None
    assert out.get('qr_png_url') is not None
    assert '/handshake/' in out['qr_url']


def test_mudar_status_outro_nao_gera_qr(app, admin_user, loja, catalogo):
    """Confirmar nao gera QR (so separar gera)."""
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja, PedidoQRCode
    from app.services import copilot

    p = PedidoLoja(loja_id=loja.id, status='pendente',
                   data_entrega=date.today())
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id,
                               receita_id=catalogo['receita'].id,
                               quantidade=1))
    db.session.commit()

    with app.test_request_context():
        out = copilot.executar_mudar_status_pedido(
            {'pedido_id': p.id, 'novo_status': 'confirmar'},
            admin_user,
        )
    assert out['ok'] is True
    assert out.get('qr_url') is None
    assert PedidoQRCode.query.filter_by(pedido_id=p.id).count() == 0

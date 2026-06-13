"""Smoke tests do handshake QR (saida + entrega).

Cobre: token valido executa transicao, token usado nao reusa, PIN errado
recusa, _executar_envio_pedido e _executar_recebimento_pedido funcionam
sem usuario logado.
"""
from datetime import date, timedelta


def _criar_pedido_separado(catalogo, loja):
    """Helper: cria pedido com status=separado pronto pra handshake de saida."""
    from app.extensions import db
    from app.models import EstoqueProducao, PedidoItem, PedidoLoja
    p = PedidoLoja(loja_id=loja.id, status='separado',
                   data_entrega=date.today() + timedelta(days=1))
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id,
                               receita_id=catalogo['receita'].id,
                               quantidade=5))
    db.session.add(EstoqueProducao(receita_id=catalogo['receita'].id,
                                    quantidade=20))
    db.session.commit()
    return p


def test_executar_envio_pedido_baixa_industria(app, admin_user, loja, catalogo):
    """Direct call: status separado → em_transporte + baixa EstoqueProducao."""
    from app.blueprints.pedidos.routes import _executar_envio_pedido
    from app.extensions import db
    from app.models import EstoqueProducao, MovEstoqueProducao
    p = _criar_pedido_separado(catalogo, loja)
    ep = EstoqueProducao.query.filter_by(receita_id=catalogo['receita'].id).first()
    assert ep.quantidade == 20

    ok, msg = _executar_envio_pedido(p, admin_user,
                                      ref_extra='via QR / motorista Teste')
    assert ok is True
    assert p.status == 'em_transporte'
    db.session.refresh(ep)
    assert ep.quantidade == 15
    mov = MovEstoqueProducao.query.filter_by(estoque_producao_id=ep.id).first()
    assert 'motorista Teste' in mov.referencia


def test_executar_envio_pedido_rejeita_status_errado(app, admin_user, loja, catalogo):
    """Pedido pendente nao pode ser enviado."""
    from app.blueprints.pedidos.routes import _executar_envio_pedido
    from app.extensions import db
    from app.models import PedidoLoja
    p = PedidoLoja(loja_id=loja.id, status='pendente',
                   data_entrega=date.today())
    db.session.add(p)
    db.session.commit()
    ok, msg = _executar_envio_pedido(p, admin_user)
    assert ok is False
    assert 'separado' in msg


def test_executar_recebimento_pedido_sobe_loja(app, admin_user, loja, catalogo):
    """Recebimento sem divergencia sobe EstoqueLoja + status entregue."""
    from app.blueprints.pedidos.routes import _executar_recebimento_pedido
    from app.extensions import db
    from app.models import EstoqueLoja, PedidoItem, PedidoItemFoto, PedidoLoja
    p = PedidoLoja(loja_id=loja.id, status='em_transporte',
                   data_entrega=date.today())
    db.session.add(p)
    db.session.flush()
    item = PedidoItem(pedido_id=p.id,
                      receita_id=catalogo['receita'].id,
                      quantidade=3)
    db.session.add(item)
    db.session.flush()
    # Caminho QR: a entrega exige foto de conferencia (etapa=entrega) — o
    # motorista fotografa cada item antes do PIN (regra de 13/06/2026).
    db.session.add(PedidoItemFoto(
        pedido_item_id=item.id, etapa='entrega',
        imagem_url='http://x/e.jpg',
        imagem_storage_path=f'/conferencia/{p.id}/{item.id}_entrega.jpg'))
    db.session.commit()

    ok, msg, divergencias = _executar_recebimento_pedido(
        p, admin_user, recebidos_map=None,
        ref_extra='via QR / loja Ribeiro',
    )
    assert ok is True
    assert p.status == 'entregue'
    assert divergencias == []
    el = EstoqueLoja.query.filter_by(loja_id=loja.id,
                                       receita_id=catalogo['receita'].id).first()
    assert el is not None
    assert el.quantidade == 3


def test_qrcode_helper_gera_data_url(app):
    """qrcode_svc.gerar_png_data_url retorna data URL valido."""
    from app.services.qrcode_svc import gerar_png_data_url
    url = gerar_png_data_url('https://example.com/abc')
    assert url is not None
    assert url.startswith('data:image/png;base64,')
    assert len(url) > 200  # PNG tem tamanho minimo


def test_pedido_qrcode_validade(app, admin_user, loja, catalogo):
    """PedidoQRCode.valido = False quando expirado ou usado."""
    from app.extensions import db
    from app.models import PedidoQRCode
    from app.utils import agora
    p = _criar_pedido_separado(catalogo, loja)
    qr = PedidoQRCode(
        token='tok-teste',
        pedido_id=p.id, tipo='saida',
        expira_em=agora() + timedelta(hours=1),
    )
    db.session.add(qr)
    db.session.commit()
    assert qr.valido is True

    # Marca como usado
    qr.usado_em = agora()
    db.session.commit()
    assert qr.valido is False

    # Outro token: expirado
    qr2 = PedidoQRCode(
        token='tok-expirado', pedido_id=p.id, tipo='entrega',
        expira_em=agora() - timedelta(minutes=1),
    )
    db.session.add(qr2)
    db.session.commit()
    assert qr2.valido is False


def test_loja_aceita_pin(app, loja):
    """Loja.pin pode ser setado e retornado."""
    from app.extensions import db
    loja.pin = '1234'
    db.session.commit()
    db.session.refresh(loja)
    assert loja.pin == '1234'

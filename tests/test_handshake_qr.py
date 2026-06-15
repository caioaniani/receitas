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


# -------- PRG + idempotencia (14/06/2026, bug pedido 201 Nebraska 294) ---
#
# Cenario real: funcionario na loja confirmou o pedido (fotos + PIN). UI
# mostrou erro "QR Code esta ja usado" — mas o pedido virou entregue.
# Causa: refresh do navegador (pull-to-refresh do mobile, F5, voltar/avancar)
# OU double-tap no botao em rede lenta dispara um SEGUNDO POST. O primeiro
# ja consumiu o QR; o segundo cai em "QR ja usado".
#
# Fix: 1) PRG — POST com sucesso redireciona 303 pra GET de sucesso;
#      2) Idempotencia — re-POST com qr.usado_em recente (<10min) volta
#         pra tela de sucesso ao inves de mostrar erro.

def _criar_em_transporte_com_qr(catalogo, loja, app):
    """Cria pedido em_transporte + QR de entrega ativo + 1 foto de
    conferencia (pra _executar_recebimento_pedido nao bloquear)."""
    from datetime import timedelta

    from app.extensions import db
    from app.models import (
        EstoqueLoja,
        PedidoItem,
        PedidoItemFoto,
        PedidoLoja,
        PedidoQRCode,
    )
    from app.utils import agora

    loja.pin = '4321'
    p = PedidoLoja(loja_id=loja.id, status='em_transporte',
                   data_entrega=date.today())
    db.session.add(p)
    db.session.flush()
    item = PedidoItem(pedido_id=p.id,
                       receita_id=catalogo['receita'].id,
                       quantidade=3)
    db.session.add(item)
    db.session.flush()
    # Foto de conferencia (cumpre a regra "entrega exige 1 foto")
    db.session.add(PedidoItemFoto(pedido_item_id=item.id, etapa='entrega',
                                   imagem_url='https://x/y.jpg'))
    # Estoque inicial pra checar duplicacao
    db.session.add(EstoqueLoja(loja_id=loja.id,
                                receita_id=catalogo['receita'].id,
                                quantidade=0))
    qr = PedidoQRCode(token='tok-pedido-201', pedido_id=p.id, tipo='entrega',
                      expira_em=agora() + timedelta(hours=2))
    db.session.add(qr)
    db.session.commit()
    return p, qr


def test_post_sucesso_redireciona_303_pra_tela_de_sucesso(app, admin_user,
                                                            loja, catalogo):
    """PRG: POST com sucesso devolve 303 + Location pra /handshake/<tok>/
    sucesso. Refresh do navegador la = re-GET idempotente."""
    p, qr = _criar_em_transporte_com_qr(catalogo, loja, app)
    client = app.test_client()
    resp = client.post(f'/handshake/{qr.token}', data={'pin': '4321'})
    assert resp.status_code == 303, f'esperava 303, foi {resp.status_code}'
    assert '/sucesso' in (resp.headers.get('Location') or '')
    # Estoque subiu UMA vez
    from app.models import EstoqueLoja
    el = EstoqueLoja.query.filter_by(loja_id=loja.id,
                                       receita_id=catalogo['receita'].id).first()
    assert el.quantidade == 3
    assert p.status == 'entregue'


def test_get_sucesso_renderiza_sem_re_executar(app, admin_user, loja, catalogo):
    """GET na rota de sucesso so renderiza — nao re-roda o executor."""
    from app.extensions import db
    from app.models import EstoqueLoja
    from app.utils import agora
    p, qr = _criar_em_transporte_com_qr(catalogo, loja, app)
    # Simula handshake ja concluido (POST anterior)
    qr.usado_em = agora()
    qr.usado_por_descricao = f'loja:{loja.nome}'
    p.status = 'entregue'
    el = EstoqueLoja.query.filter_by(loja_id=loja.id,
                                       receita_id=catalogo['receita'].id).first()
    el.quantidade = 3  # ja subiu uma vez
    db.session.commit()

    client = app.test_client()
    resp = client.get(f'/handshake/{qr.token}/sucesso')
    assert resp.status_code == 200
    assert b'Entrega confirmada' in resp.data or b'confirmada' in resp.data.lower()
    # Estoque NAO duplicou
    db.session.refresh(el)
    assert el.quantidade == 3


def test_get_sucesso_sem_uso_volta_pro_form(app, admin_user, loja, catalogo):
    """Se o QR ainda nao foi consumido, /sucesso volta pro form."""
    p, qr = _criar_em_transporte_com_qr(catalogo, loja, app)
    client = app.test_client()
    resp = client.get(f'/handshake/{qr.token}/sucesso')
    assert resp.status_code == 302
    assert f'/handshake/{qr.token}' in (resp.headers.get('Location') or '')


def test_re_post_apos_uso_redireciona_pra_sucesso_em_vez_de_erro(
        app, admin_user, loja, catalogo):
    """A regressao do pedido 201: usuario refresh / double-tap.

    Re-POST com qr.usado_em recente deve redirecionar 303 pra sucesso —
    NUNCA mostrar 'QR Code esta ja usado'."""
    from app.extensions import db
    from app.models import EstoqueLoja, HandshakeAudit
    from app.utils import agora
    p, qr = _criar_em_transporte_com_qr(catalogo, loja, app)
    # Simula que o primeiro POST ja deu sucesso 30s atras
    qr.usado_em = agora()
    qr.usado_por_descricao = f'loja:{loja.nome}'
    p.status = 'entregue'
    el = EstoqueLoja.query.filter_by(loja_id=loja.id,
                                       receita_id=catalogo['receita'].id).first()
    el.quantidade = 3
    db.session.commit()

    client = app.test_client()
    resp = client.post(f'/handshake/{qr.token}', data={'pin': '4321'})
    # NUNCA 410 com "QR ja usado" — sempre 303 pra sucesso na janela
    assert resp.status_code == 303
    assert '/sucesso' in (resp.headers.get('Location') or '')
    # Estoque NAO duplicou
    db.session.refresh(el)
    assert el.quantidade == 3
    # Auditoria registra o supress
    audits = HandshakeAudit.query.filter_by(
        token=qr.token, etapa='double_submit_suprimido').all()
    assert len(audits) >= 1


def test_re_post_apos_janela_de_10min_volta_a_mostrar_erro(
        app, admin_user, loja, catalogo):
    """Reuso INTENCIONAL muito depois do fato (>10min) ainda mostra o
    alarme original — sem mascarar tentativa de fraude."""
    from datetime import timedelta

    from app.extensions import db
    from app.utils import agora
    p, qr = _criar_em_transporte_com_qr(catalogo, loja, app)
    qr.usado_em = agora() - timedelta(minutes=30)
    qr.usado_por_descricao = f'loja:{loja.nome}'
    p.status = 'entregue'
    db.session.commit()

    client = app.test_client()
    resp = client.post(f'/handshake/{qr.token}', data={'pin': '4321'})
    assert resp.status_code == 410
    assert b'ja usado' in resp.data or 'já usado'.encode() in resp.data


def test_template_confirmar_trava_double_click_no_botao():
    """Trava regressao: o form de PIN tem onsubmit que desabilita o botao
    no primeiro clique (defesa contra double-tap em rede lenta)."""
    import pathlib
    src = pathlib.Path('app/templates/handshake/confirmar.html').read_text()
    assert 'travarBotaoConfirmar' in src
    assert 'btn.disabled = true' in src

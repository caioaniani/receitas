"""Entrega exige foto de comprovacao (decisao do dono 13/06/2026).

O recebimento manual ('Confirmar Recebimento' na ficha) aceitava entregar
sem foto — pedido #197 ficou entregue sem comprovacao. Agora o executor
`_executar_recebimento_pedido` RECUSA se nao houver nenhuma das tres fontes
de prova: foto nova (upload), PedidoItemFoto da conferencia de entrega
(caminho QR), ou FotoRecebimento previa. Sem foto = nao entrega.
"""
from unittest.mock import patch


def _pedido_em_transporte(app):
    from app.extensions import db
    from app.models import Loja, PedidoItem, PedidoLoja, Receita
    loja = Loja(nome='Ribeiro do Vale', ativa=True)
    r = Receita(nome='Pain au Chocolat', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=80.0)
    db.session.add_all([loja, r])
    db.session.flush()
    p = PedidoLoja(loja_id=loja.id, status='em_transporte')
    db.session.add(p)
    db.session.flush()
    item = PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=6)
    db.session.add(item)
    db.session.commit()
    return p, item


def test_recebimento_sem_foto_e_recusado(app):
    """Caso #197: confirmar sem anexar foto → recusa, status intacto."""
    from app.blueprints.pedidos.routes import _executar_recebimento_pedido
    p, _ = _pedido_em_transporte(app)
    ok, msg, _div = _executar_recebimento_pedido(p, user=None)
    assert ok is False
    assert 'foto' in msg.lower()
    assert p.status == 'em_transporte'   # NAO entregou


def test_recebimento_com_foto_nova_entrega(app):
    """Anexar foto no upload manual → entrega normalmente."""
    from app.blueprints.pedidos.routes import _executar_recebimento_pedido
    p, _ = _pedido_em_transporte(app)
    fotos = [{'imagem': b'\xff\xd8\xff\xfake-jpeg', 'mimetype': 'image/jpeg'}]
    with patch('app.services.dropbox_storage.disponivel', return_value=False), \
         patch('app.services.pedidos_notificacao.notificar_pedido_recebido'):
        ok, _msg, _div = _executar_recebimento_pedido(p, user=None, fotos=fotos)
    assert ok is True
    assert p.status == 'entregue'


def test_caminho_qr_nao_quebra_com_foto_de_conferencia(app):
    """O QR salva PedidoItemFoto (etapa=entrega) ANTES de chamar o executor
    e NAO passa `fotos` aqui. A nova regra precisa aceitar esse caminho —
    senao a entrega via QR quebraria."""
    from app.blueprints.pedidos.routes import _executar_recebimento_pedido
    from app.extensions import db
    from app.models import PedidoItemFoto
    p, item = _pedido_em_transporte(app)
    db.session.add(PedidoItemFoto(
        pedido_item_id=item.id, etapa='entrega',
        imagem_url='http://x/c.jpg',
        imagem_storage_path=f'/conferencia/{p.id}/{item.id}_entrega.jpg'))
    db.session.commit()
    with patch('app.services.pedidos_notificacao.notificar_pedido_recebido'):
        ok, _msg, _div = _executar_recebimento_pedido(
            p, user=None, ref_extra='via QR / loja Ribeiro do Vale')
    assert ok is True
    assert p.status == 'entregue'


def test_foto_de_conferencia_de_SAIDA_nao_conta_pra_entrega(app):
    """So foto de ENTREGA comprova entrega. Foto de saida (industria) nao
    deve liberar o recebimento — senao entregaria com a prova errada."""
    from app.blueprints.pedidos.routes import _executar_recebimento_pedido
    from app.extensions import db
    from app.models import PedidoItemFoto
    p, item = _pedido_em_transporte(app)
    db.session.add(PedidoItemFoto(
        pedido_item_id=item.id, etapa='saida',   # SAIDA, nao entrega
        imagem_url='http://x/s.jpg',
        imagem_storage_path=f'/conferencia/{p.id}/{item.id}_saida.jpg'))
    db.session.commit()
    ok, msg, _div = _executar_recebimento_pedido(p, user=None)
    assert ok is False
    assert 'foto' in msg.lower()
    assert p.status == 'em_transporte'


def test_rota_receber_sem_foto_mostra_erro(app):
    """Integracao: POST /pedidos/<id>/receber sem foto → flash de erro,
    status intacto (a rota propaga a recusa do executor)."""
    from app.extensions import db
    from app.models import Usuario
    p, _ = _pedido_em_transporte(app)
    admin = Usuario(nome='Admin', login='admin_rf', papel='admin')
    admin.set_senha('123')
    db.session.add(admin)
    db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin_rf', 'senha': '123'})
    r = c.post(f'/pedidos/{p.id}/receber', data={}, follow_redirects=True)
    assert r.status_code == 200
    assert 'foto' in r.data.decode().lower()
    from app.models import PedidoLoja
    assert db.session.get(PedidoLoja, p.id).status == 'em_transporte'


def test_template_input_foto_required(app):
    """A ficha do pedido em_transporte exige foto no front (required)
    quando ainda nao ha foto de conferencia de entrega."""
    from app.extensions import db
    from app.models import Usuario
    p, _ = _pedido_em_transporte(app)
    admin = Usuario(nome='Admin', login='admin_rf2', papel='admin')
    admin.set_senha('123')
    db.session.add(admin)
    db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin_rf2', 'senha': '123'})
    body = c.get(f'/pedidos/{p.id}').data.decode()
    assert 'name="fotos"' in body
    assert 'required' in body
    assert 'obrigat' in body.lower()

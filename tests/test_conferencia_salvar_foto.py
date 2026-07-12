"""`conferencia.salvar_foto` — o caminho de FOTO NOVA do fluxo QR.

Regressão do M6 Commit D (12/07/2026): a coluna BLOB `imagem` saiu do
modelo, mas o construtor de foto nova ainda passava `imagem=None` —
TypeError em TODA primeira foto de item via QR (o handshake devolvia
"erro ao salvar a foto" sempre e a conferência nunca fechava). O caminho
não tinha NENHUM teste; a suíte verde não pegou.
"""
import io
from unittest.mock import patch

from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models import Loja, PedidoItem, PedidoItemFoto, PedidoLoja, Receita
from app.services import conferencia


def _item(app):
    loja = Loja(nome='Ribeiro do Vale', ativa=True)
    r = Receita(nome='Croissant Tradicional', categoria='Paes',
                rendimento_qtd=1, rendimento_unidade='un', peso_base=80.0)
    db.session.add_all([loja, r])
    db.session.flush()
    p = PedidoLoja(loja_id=loja.id, status='em_transporte')
    db.session.add(p)
    db.session.flush()
    item = PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=6)
    db.session.add(item)
    db.session.commit()
    return item


def _arquivo():
    return FileStorage(stream=io.BytesIO(b'\xff\xd8\xff\xfake-jpeg'),
                       filename='foto.jpg', content_type='image/jpeg')


def _mocks():
    return (
        patch('app.services.dropbox_storage.disponivel', return_value=True),
        patch('app.utils.comprimir_imagem', side_effect=lambda b: b),
        patch('app.services.dropbox_storage.upload_publico',
              return_value={'url': 'http://x/c.jpg',
                            'storage_path': '/conferencia/1/1_saida.jpg'}),
    )


def test_salvar_foto_nova_cria_registro_dropbox(app):
    item = _item(app)
    m1, m2, m3 = _mocks()
    with m1, m2, m3:
        foto, erro = conferencia.salvar_foto(item.id, 'saida', _arquivo())
    assert erro is None
    assert foto is not None and foto.imagem_url == 'http://x/c.jpg'
    assert PedidoItemFoto.query.filter_by(pedido_item_id=item.id).count() == 1


def test_salvar_foto_substitui_existente(app):
    item = _item(app)
    m1, m2, m3 = _mocks()
    with m1, m2, m3:
        primeira, _ = conferencia.salvar_foto(item.id, 'saida', _arquivo())
        segunda, erro = conferencia.salvar_foto(item.id, 'saida', _arquivo())
    assert erro is None
    assert segunda.id == primeira.id     # substitui, nao duplica
    assert PedidoItemFoto.query.filter_by(pedido_item_id=item.id).count() == 1

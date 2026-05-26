"""Pedido pode ter Produto como item, nao so receita/MP. Bug: o picker de
/pedidos/novo e /pedidos/<id>/editar so listava receitas e materias-primas
(o POST tambem zerava produto_id), entao nao dava pra pedir um produto
cadastrado pra industria entregar."""
from datetime import timedelta

import pytest


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente):
    return cliente.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def test_picker_do_novo_lista_produtos(app, admin_user, loja, catalogo, cliente):
    _login(cliente)
    r = cliente.get('/pedidos/novo')
    assert r.status_code == 200
    assert b'<optgroup label="Produtos">' in r.data
    assert ('p_%d' % catalogo['produto'].id).encode() in r.data


def test_novo_cria_pedido_com_produto(app, admin_user, loja, catalogo, cliente):
    from app.models import PedidoItem
    from app.utils import hoje
    _login(cliente)
    amanha = (hoje() + timedelta(days=1)).isoformat()
    pid = catalogo['produto'].id
    r = cliente.post('/pedidos/novo', data={
        'loja_id': loja.id,
        'data_entrega': amanha,
        'observacao': '',
        'item_id[]': 'p_%d' % pid,
        'item_qtd[]': '5',
        'item_obs[]': '',
        'item_estado[]': '',
    })
    assert r.status_code == 302
    item = PedidoItem.query.filter_by(produto_id=pid).first()
    assert item is not None
    assert item.quantidade == 5
    assert item.receita_id is None
    assert item.materia_prima_id is None


def test_editar_mostra_produto_selecionado(app, admin_user, loja, catalogo, cliente):
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    from app.utils import hoje
    p = PedidoLoja(loja_id=loja.id, data_entrega=hoje() + timedelta(days=1),
                   status='confirmado', criado_por=admin_user.id)
    db.session.add(p)
    db.session.commit()
    pid = catalogo['produto'].id
    db.session.add(PedidoItem(pedido_id=p.id, produto_id=pid, quantidade=3))
    db.session.commit()
    _login(cliente)
    r = cliente.get(f'/pedidos/{p.id}/editar')
    assert r.status_code == 200
    assert ('p_%d" selected' % pid).encode() in r.data

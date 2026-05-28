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


def test_picker_do_novo_oferece_produtos(app, admin_user, loja, catalogo, cliente):
    """O picker do /novo (agora typeahead) oferece produtos, nao so receitas/MP.
    A pagina traz o campo de busca e o endpoint retorna o produto com id
    p_<id> (que casa com _parse_item_id no POST)."""
    _login(cliente)
    r = cliente.get('/pedidos/novo')
    assert r.status_code == 200
    assert b'item-busca' in r.data  # widget de busca por digitacao presente
    # o endpoint que alimenta o typeahead acha o produto do catalogo
    termo = catalogo['produto'].nome.split()[0]
    busca = cliente.get('/pedidos/buscar-itens.json?q=' + termo)
    ids = [i['id'] for i in busca.get_json()['itens']]
    assert ('p_%d' % catalogo['produto'].id) in ids


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


def test_admin_cria_pedido_loja_mesmo_dia(app, admin_user, loja, catalogo, cliente):
    """Admin pode criar pedido de loja pra entregar HOJE (mesmo dia)."""
    from app.models import PedidoLoja
    from app.utils import hoje
    _login(cliente)
    r = cliente.post('/pedidos/novo', data={
        'loja_id': loja.id,
        'data_entrega': hoje().isoformat(),   # mesmo dia
        'observacao': '',
        'item_id[]': 'r_%d' % catalogo['receita'].id,
        'item_qtd[]': '3',
        'item_obs[]': '',
        'item_estado[]': '',
    })
    assert r.status_code == 302  # criou, nao bloqueou
    assert PedidoLoja.query.filter_by(loja_id=loja.id, data_entrega=hoje()).first() is not None


def test_form_novo_min_hoje_pra_admin(app, admin_user, loja, catalogo, cliente):
    """O campo de data do /novo aceita hoje pra admin (min = hoje)."""
    from app.utils import hoje
    _login(cliente)
    r = cliente.get('/pedidos/novo')
    assert r.status_code == 200
    assert ('min="%s"' % hoje().isoformat()).encode() in r.data

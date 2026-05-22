"""Testes da rota /pedidos/<id>/editar (UI HTTP).

Cobre:
- GET renderiza form com dados atuais
- POST aplica mudancas (data + obs + REPLACE de itens com estado)
- Bloqueio por status (redirect pra detalhe sem editar)
- Rejeicao de POST sem itens
"""
from datetime import date, timedelta

import pytest


def _login(cliente):
    return cliente.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def _pedido_pendente(loja, admin_user, catalogo, status='pendente'):
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    p = PedidoLoja(loja_id=loja.id, status=status,
                   data_entrega=date.today() + timedelta(days=1),
                   criado_por=admin_user.id)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id,
                              receita_id=catalogo['receita'].id,
                              quantidade=10))
    db.session.commit()
    return p


@pytest.fixture
def cliente(app):
    return app.test_client()


def test_editar_get_renderiza_form_com_dados_atuais(
        app, cliente, admin_user, loja, catalogo):
    """GET /pedidos/<id>/editar autenticado: 200 + form preenchido."""
    p = _pedido_pendente(loja, admin_user, catalogo)
    _login(cliente)

    r = cliente.get(f'/pedidos/{p.id}/editar')
    assert r.status_code == 200
    assert b'Editar Pedido' in r.data
    # Item atual (qtd=10) aparece pre-preenchido no value do input
    assert b'value="10"' in r.data


def test_editar_post_replace_itens_persiste(
        app, cliente, admin_user, loja, catalogo):
    """POST atualiza data + obs + REPLACE itens com estado='backup'."""
    from app.models import PedidoItem, PedidoLoja
    p = _pedido_pendente(loja, admin_user, catalogo)
    _login(cliente)

    nova_data = (date.today() + timedelta(days=3)).strftime('%Y-%m-%d')
    r = cliente.post(f'/pedidos/{p.id}/editar', data={
        'data_entrega': nova_data,
        'observacao': 'editado pelo teste',
        'item_id[]': [f'r_{catalogo["receita"].id}'],
        'item_qtd[]': ['20'],
        'item_estado[]': ['backup'],
        'item_obs[]': [''],
    })
    assert r.status_code == 302
    assert r.headers['Location'].endswith(f'/pedidos/{p.id}')

    p2 = PedidoLoja.query.get(p.id)
    assert p2.data_entrega.strftime('%Y-%m-%d') == nova_data
    assert p2.observacao == 'editado pelo teste'

    itens = PedidoItem.query.filter_by(pedido_id=p.id).all()
    assert len(itens) == 1
    assert itens[0].quantidade == 20
    assert itens[0].estado == 'backup'


def test_editar_get_bloqueado_se_status_separado(
        app, cliente, admin_user, loja, catalogo):
    """Pedido em 'separado' redireciona pra detalhe sem renderizar form."""
    p = _pedido_pendente(loja, admin_user, catalogo, status='separado')
    _login(cliente)

    r = cliente.get(f'/pedidos/{p.id}/editar', follow_redirects=False)
    assert r.status_code == 302
    assert r.headers['Location'].endswith(f'/pedidos/{p.id}')
    # NAO pode redirecionar pra /editar (loop)
    assert '/editar' not in r.headers['Location']


def test_editar_post_zero_itens_rejeita(
        app, cliente, admin_user, loja, catalogo):
    """POST sem nenhum item valido nao deve commitar; banco mantem itens."""
    from app.models import PedidoItem
    p = _pedido_pendente(loja, admin_user, catalogo)
    _login(cliente)

    nova_data = (date.today() + timedelta(days=3)).strftime('%Y-%m-%d')
    r = cliente.post(f'/pedidos/{p.id}/editar', data={
        'data_entrega': nova_data,
        'observacao': 'tentativa',
        # sem item_id[]/item_qtd[]
    })
    assert r.status_code == 302
    # Redirect de volta pra /editar (mesma pagina) sinaliza rejeicao
    assert '/editar' in r.headers['Location']

    # Banco mantem o item original
    itens = PedidoItem.query.filter_by(pedido_id=p.id).all()
    assert len(itens) == 1
    assert itens[0].quantidade == 10

"""Auto-refresh da TV do padeiro: /padeiro/listas.html devolve so o fragmento
dos cards (pra TV trocar sozinha sem recarregar a pagina e sem reiniciar o som)."""
import pytest


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente):
    return cliente.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def _pedido_hoje(app, admin_user, loja, catalogo):
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    from app.utils import hoje
    p = PedidoLoja(loja_id=loja.id, data_entrega=hoje(), status='confirmado',
                   criado_por=admin_user.id)
    db.session.add(p)
    db.session.commit()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=catalogo['receita'].id,
                              quantidade=4, estado='assado'))
    db.session.commit()
    return p


def test_listas_html_devolve_so_o_fragmento(app, admin_user, loja, catalogo, cliente):
    _pedido_hoje(app, admin_user, loja, catalogo)
    _login(cliente)
    r = cliente.get('/padeiro/listas.html')
    assert r.status_code == 200
    body = r.data
    # tem os cards (loja, item com estado, botao separar)...
    assert loja.nome.encode() in body
    assert b'[ASSADO]' in body
    assert b'SEPARAR' in body
    # ...mas NAO o esqueleto da pagina (e fragmento, nao recarrega a TV inteira)
    assert b'<!doctype' not in body.lower()
    assert b'id="listas"' not in body
    assert b'Ativar o som' not in body


def test_index_inclui_o_fragmento(app, admin_user, loja, catalogo, cliente):
    _pedido_hoje(app, admin_user, loja, catalogo)
    _login(cliente)
    r = cliente.get('/padeiro/')
    assert r.status_code == 200
    # a pagina cheia tem o container que o JS troca + os cards do fragmento
    assert b'id="listas"' in r.data
    assert loja.nome.encode() in r.data
    assert b'SEPARAR' in r.data

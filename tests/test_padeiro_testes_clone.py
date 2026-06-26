"""Smoke test do clone /padeiro-testes (Etapa 2).

Confirma que o clone renderiza e que a /padeiro OFICIAL segue intacta.
"""
import pytest


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente):
    return cliente.post('/auth/login',
                        data={'login': 'admin', 'senha': '123'})


def test_padeiro_testes_index_renderiza(app, admin_user, loja, catalogo, cliente):
    _login(cliente)
    r = cliente.get('/padeiro-testes/')
    assert r.status_code == 200


def test_padeiro_testes_listas_fragmento(app, admin_user, loja, catalogo, cliente):
    _login(cliente)
    r = cliente.get('/padeiro-testes/listas.html')
    assert r.status_code == 200


def test_padeiro_oficial_intacto(app, admin_user, loja, catalogo, cliente):
    _login(cliente)
    r = cliente.get('/padeiro/')
    assert r.status_code == 200

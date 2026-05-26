"""Avisos pra producao: qualquer usuario cria; aparece na TV do padeiro
(/padeiro/avisos.json) e some quando confirmado."""
import pytest


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente):
    return cliente.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def test_criar_e_consumir_aviso(app, admin_user, cliente):
    from app.models import Aviso
    _login(cliente)

    r = cliente.post('/avisos/', data={'texto': 'Falta farinha'})
    assert r.status_code == 302
    assert Aviso.query.count() == 1

    j = cliente.get('/padeiro/avisos.json').get_json()
    assert len(j['avisos']) == 1
    assert j['avisos'][0]['texto'] == 'Falta farinha'
    aid = j['avisos'][0]['id']

    assert cliente.post(f'/padeiro/avisos/{aid}/confirmar').status_code == 200
    assert cliente.get('/padeiro/avisos.json').get_json()['avisos'] == []
    a = Aviso.query.get(aid)
    assert a.confirmado and a.confirmado_por_id == admin_user.id


def test_aviso_vazio_nao_cria(app, admin_user, cliente):
    from app.models import Aviso
    _login(cliente)
    cliente.post('/avisos/', data={'texto': '   '})
    assert Aviso.query.count() == 0

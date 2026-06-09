"""Painel do Dia — tela simples da equipe + alerta de pedido novo do dia."""
from unittest.mock import patch

import pytest


@pytest.fixture
def admin_logado(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Gerente', login='ger', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(u.id)
        sess['_fresh'] = True
    return client


def _pedido(code, cartinha='', nome='Cliente'):
    return {
        'code': code, 'destinatario': nome, 'comprador': nome,
        'endereco': 'Rua X, 10', 'periodo': '8h às 9h', 'telefone': '11999',
        'cartinha_vnda': cartinha,
        'itens': [{'nome': 'Family Box', 'quantidade': 1}],
    }


def test_painel_html_carrega(admin_logado):
    r = admin_logado.get('/entregas/painel')
    assert r.status_code == 200
    assert b'PEDIDOS DE HOJE' in r.data
    assert b'LIGAR PAINEL' in r.data


def test_api_painel_marca_novos_e_resolve_cartinha(admin_logado):
    with patch('app.services.vnda.buscar_pedidos_do_dia',
               return_value={'pedidos': [_pedido('AB1', 'Feliz aniversário')]}):
        r = admin_logado.get('/entregas/api/painel')
    data = r.get_json()
    assert data['total'] == 1
    assert data['novos'] == 1
    p = data['pedidos'][0]
    assert p['code'] == 'AB1'
    assert p['novo'] is True
    assert p['cartinha'] == 'Feliz aniversário'   # caiu do cartinha_vnda


def test_cartinha_manual_tem_prioridade(admin_logado):
    from app.extensions import db
    from app.models import CartinhaEntrega
    db.session.add(CartinhaEntrega(pedido_code='AB1', texto='Texto corrigido'))
    db.session.commit()
    with patch('app.services.vnda.buscar_pedidos_do_dia',
               return_value={'pedidos': [_pedido('AB1', 'Texto do VNDA')]}):
        r = admin_logado.get('/entregas/api/painel')
    p = r.get_json()['pedidos'][0]
    assert p['cartinha'] == 'Texto corrigido'   # manual > VNDA


def test_visto_silencia_o_pedido(admin_logado):
    with patch('app.services.vnda.buscar_pedidos_do_dia',
               return_value={'pedidos': [_pedido('AB1')]}):
        assert admin_logado.get('/entregas/api/painel').get_json()['pedidos'][0]['novo'] is True
        rv = admin_logado.post('/entregas/api/painel/visto/AB1')
        assert rv.get_json()['ok'] is True
        depois = admin_logado.get('/entregas/api/painel').get_json()
        assert depois['pedidos'][0]['novo'] is False
        assert depois['novos'] == 0


def test_visto_idempotente(admin_logado):
    from app.models import PedidoVistoPainel
    admin_logado.post('/entregas/api/painel/visto/AB1')
    admin_logado.post('/entregas/api/painel/visto/AB1')
    assert PedidoVistoPainel.query.filter_by(pedido_code='AB1').count() == 1


def test_painel_exige_login(app):
    """Sem login, a tela e a API negam."""
    c = app.test_client()
    assert c.get('/entregas/painel').status_code in (302, 401)
    assert c.get('/entregas/api/painel').status_code in (302, 401)

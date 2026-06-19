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
        p0 = admin_logado.get('/entregas/api/painel').get_json()['pedidos'][0]
        assert p0['novo'] is True and p0['status'] == 'novo'
        rv = admin_logado.post('/entregas/api/painel/status/AB1?status=visto')
        assert rv.get_json()['ok'] is True
        depois = admin_logado.get('/entregas/api/painel').get_json()
        assert depois['pedidos'][0]['novo'] is False
        assert depois['pedidos'][0]['status'] == 'visto'
        assert depois['novos'] == 0


def test_fluxo_status_pronto_entregue(admin_logado):
    """novo → visto → pronto → entregue, e o expresso vem marcado."""
    with patch('app.services.vnda.buscar_pedidos_do_dia',
               return_value={'pedidos': [_pedido('AB1')]}):
        admin_logado.post('/entregas/api/painel/status/AB1?status=pronto')
        p = admin_logado.get('/entregas/api/painel').get_json()['pedidos'][0]
        assert p['status'] == 'pronto'
        admin_logado.post('/entregas/api/painel/status/AB1?status=entregue')
        p = admin_logado.get('/entregas/api/painel').get_json()['pedidos'][0]
        assert p['status'] == 'entregue'


def test_api_painel_devolve_csrf_fresco(admin_logado):
    """O painel renova o token CSRF a cada poll. Sem isso, o token gerado no
    load expira em 1h (default Flask-WTF) e, no display sempre-ligado da
    cozinha, os POSTs de status passam a falhar em silêncio → status muda na
    tela mas não salva e 'volta' no reload."""
    with patch('app.services.vnda.buscar_pedidos_do_dia',
               return_value={'pedidos': []}):
        j = admin_logado.get('/entregas/api/painel').get_json()
    assert j.get('csrf')  # token presente e não vazio a cada poll


def test_status_invalido_recusado(admin_logado):
    r = admin_logado.post('/entregas/api/painel/status/AB1?status=banana')
    assert r.status_code == 400


def test_visto_nao_rebaixa_pronto(admin_logado):
    """Clique de abertura (visto) NAO deve rebaixar um pedido ja 'pronto'."""
    admin_logado.post('/entregas/api/painel/status/AB1?status=pronto')
    admin_logado.post('/entregas/api/painel/status/AB1?status=visto')
    from app.models import PainelPedidoStatus
    s = PainelPedidoStatus.query.filter_by(pedido_code='AB1').first()
    assert s.status == 'pronto'   # manteve


def test_status_idempotente_uma_linha(admin_logado):
    from app.models import PainelPedidoStatus
    admin_logado.post('/entregas/api/painel/status/AB1?status=visto')
    admin_logado.post('/entregas/api/painel/status/AB1?status=pronto')
    assert PainelPedidoStatus.query.filter_by(pedido_code='AB1').count() == 1


def test_painel_exige_login(app):
    """Sem login, a tela e a API negam."""
    c = app.test_client()
    assert c.get('/entregas/painel').status_code in (302, 401)
    assert c.get('/entregas/api/painel').status_code in (302, 401)


def test_qualquer_logado_acessa(app):
    """Trava afrouxada: usuario comum (sem admin, sem loja) acessa o painel."""
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Padeiro', login='pad', papel='padeiro')  # sem loja_id
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as sess:
        sess['_user_id'] = str(u.id)
        sess['_fresh'] = True
    assert c.get('/entregas/painel').status_code == 200
    with patch('app.services.vnda.buscar_pedidos_do_dia',
               return_value={'pedidos': []}):
        assert c.get('/entregas/api/painel').status_code == 200

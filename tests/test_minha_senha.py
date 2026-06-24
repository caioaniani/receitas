"""Tela /auth/minha-senha — usuario logado troca a propria senha.

Regras:
- Exige login
- Exige senha atual correta
- Nova senha tem que bater com confirmacao
- Nova senha precisa de pelo menos 8 caracteres
"""
import pytest


@pytest.fixture
def cliente(app):
    return app.test_client()


@pytest.fixture
def usuario(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Z', login='zazu', papel='gerente')
    u.set_senha('senha-original')
    db.session.add(u)
    db.session.commit()
    return u


def _login(cliente, login_val, senha):
    return cliente.post('/auth/login',
                         data={'login': login_val, 'senha': senha})


def test_anonimo_redireciona_pra_login(cliente):
    r = cliente.get('/auth/minha-senha', follow_redirects=False)
    assert r.status_code in (301, 302, 308)
    assert '/auth/login' in r.headers.get('Location', '')


def test_get_renderiza_form(cliente, usuario):
    _login(cliente, 'zazu', 'senha-original')
    r = cliente.get('/auth/minha-senha')
    assert r.status_code == 200
    assert b'senha_atual' in r.data
    assert b'nova_senha' in r.data


def test_troca_com_sucesso(cliente, usuario):
    _login(cliente, 'zazu', 'senha-original')
    r = cliente.post('/auth/minha-senha', data={
        'senha_atual': 'senha-original',
        'nova_senha': 'nova-senha-forte',
        'confirma_senha': 'nova-senha-forte',
    }, follow_redirects=False)
    assert r.status_code == 302

    from app.extensions import db
    db.session.expire_all()
    from app.models import Usuario
    u = Usuario.query.get(usuario.id)
    assert u.check_senha('nova-senha-forte')
    assert not u.check_senha('senha-original')


def test_senha_atual_errada_bloqueia(cliente, usuario):
    _login(cliente, 'zazu', 'senha-original')
    r = cliente.post('/auth/minha-senha', data={
        'senha_atual': 'errado',
        'nova_senha': 'nova-senha-forte',
        'confirma_senha': 'nova-senha-forte',
    }, follow_redirects=False)
    assert r.status_code == 302

    from app.extensions import db
    db.session.expire_all()
    from app.models import Usuario
    u = Usuario.query.get(usuario.id)
    assert u.check_senha('senha-original'), 'senha foi trocada sem verificar a atual'
    assert not u.check_senha('nova-senha-forte')


def test_confirmacao_nao_bate(cliente, usuario):
    _login(cliente, 'zazu', 'senha-original')
    r = cliente.post('/auth/minha-senha', data={
        'senha_atual': 'senha-original',
        'nova_senha': 'nova-senha-a',
        'confirma_senha': 'nova-senha-b',
    }, follow_redirects=False)
    assert r.status_code == 302

    from app.extensions import db
    db.session.expire_all()
    from app.models import Usuario
    u = Usuario.query.get(usuario.id)
    assert u.check_senha('senha-original')


def test_senha_curta_bloqueia(cliente, usuario):
    _login(cliente, 'zazu', 'senha-original')
    r = cliente.post('/auth/minha-senha', data={
        'senha_atual': 'senha-original',
        'nova_senha': '123',
        'confirma_senha': '123',
    }, follow_redirects=False)
    assert r.status_code == 302

    from app.extensions import db
    db.session.expire_all()
    from app.models import Usuario
    u = Usuario.query.get(usuario.id)
    assert u.check_senha('senha-original')
    assert not u.check_senha('123')

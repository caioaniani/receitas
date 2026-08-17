"""Login, logout, rate limit, next_url.

Pega regressao em: bypass de senha, open redirect, falha de hash,
mudanca no fluxo de autenticacao.
"""
import pytest


@pytest.fixture
def cliente(app):
    return app.test_client()


@pytest.fixture
def funcionario(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Joao', login='joao', papel='funcionario')
    u.set_senha('senha-joao')
    db.session.add(u)
    db.session.commit()
    return u


def test_login_get_renderiza(cliente):
    """Tela de login responde 200 GET."""
    r = cliente.get('/auth/login')
    assert r.status_code == 200
    assert b'login' in r.data.lower()


def test_login_credenciais_validas(cliente, admin_user):
    """Login correto redireciona pra index (302) e nao mostra erro."""
    r = cliente.post('/auth/login',
                     data={'login': 'admin', 'senha': '123'},
                     follow_redirects=False)
    assert r.status_code == 302
    # Nao redireciona pra login de novo (sinal de falha)
    assert '/auth/login' not in r.headers.get('Location', '')


def test_login_senha_errada(cliente, admin_user):
    """Senha incorreta nao loga e mostra mensagem de erro."""
    r = cliente.post('/auth/login',
                     data={'login': 'admin', 'senha': 'errada'},
                     follow_redirects=True)
    assert r.status_code == 200
    assert b'incorret' in r.data.lower()


def test_login_usuario_inexistente(cliente):
    """Login que nao existe nao loga."""
    r = cliente.post('/auth/login',
                     data={'login': 'fantasma', 'senha': 'qualquer'},
                     follow_redirects=True)
    assert r.status_code == 200
    assert b'incorret' in r.data.lower()


def test_logout_invalida_sessao(cliente, admin_user):
    """Apos logout, rotas protegidas redirecionam pra login."""
    cliente.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    cliente.get('/auth/logout')
    r = cliente.get('/auth/painel', follow_redirects=False)
    assert r.status_code == 302
    assert '/auth/login' in r.headers.get('Location', '')


def test_login_next_url_externo_bloqueado(cliente, admin_user):
    """`?next=http://evil.com/` nao pode redirecionar pra fora — open redirect."""
    r = cliente.post('/auth/login?next=http://evil.com/x',
                     data={'login': 'admin', 'senha': '123'},
                     follow_redirects=False)
    assert r.status_code == 302
    location = r.headers.get('Location', '')
    assert 'evil.com' not in location, f'open redirect: {location}'


def test_login_next_url_interno_ok(cliente, admin_user):
    """`?next=/relativo` deve ser respeitado."""
    r = cliente.post('/auth/login?next=/usuarios',
                     data={'login': 'admin', 'senha': '123'},
                     follow_redirects=False)
    assert r.status_code == 302
    assert '/usuarios' in r.headers.get('Location', '')


def test_funcionario_login_vai_pra_landing(cliente, funcionario):
    """Funcionario apos login cai no index, que renderiza a landing didatica
    (2 cards) — nao mais minhas_fichas, e nem a home de admin."""
    r = cliente.post('/auth/login',
                     data={'login': 'joao', 'senha': 'senha-joao'},
                     follow_redirects=False)
    assert r.status_code == 302
    location = r.headers.get('Location', '')
    assert 'minhas-fichas' not in location
    assert '/padeiro' not in location
    # segue pro index: landing de 2 cards, nao a home de admin (hero do copilot)
    r2 = cliente.get('/', follow_redirects=False)
    assert r2.status_code == 200
    assert b'Fazer novo pedido' in r2.data
    assert b'home-copilot-form' not in r2.data


def test_hash_de_senha_nao_e_o_proprio_texto(app):
    """check_senha valida via hash, set_senha nao armazena plaintext."""
    from app.models import Usuario
    u = Usuario(nome='X', login='x', papel='funcionario')
    u.set_senha('senha-secreta')
    assert u.senha_hash != 'senha-secreta'
    assert u.check_senha('senha-secreta')
    assert not u.check_senha('outra-coisa')
    assert not u.check_senha('')

def test_preview_admin_password_redefine_admin(app, admin_user, monkeypatch):
    """Senha de preview so e aplicada quando o modo preview esta explicito."""
    from app import _criar_admin
    from app.extensions import db

    assert not admin_user.check_senha('senha-da-previa')
    monkeypatch.setenv('PREVIEW_MODE', '1')
    monkeypatch.setenv('PREVIEW_ADMIN_PASSWORD', 'senha-da-previa')

    _criar_admin()
    db.session.refresh(admin_user)

    assert admin_user.check_senha('senha-da-previa')
    assert admin_user.senha_provisoria is False

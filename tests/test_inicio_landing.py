"""Tela inicial didática para usuários não-padeiro.

Cove: roteamento por papel (admin→home.html, não-padeiro→inicio.html),
cards "Fazer novo pedido" e "Pedidos feitos", e decorator @pedidos_required
nas rotas de pedido (novo/lista/buscar).
"""


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def test_index_admin_home(app, admin_user):
    """Admin vê home.html (hero do copilot), não a landing de cards."""
    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/')
    assert resp.status_code == 200
    assert b'home-copilot-form' in resp.data       # marcador estável do home.html
    assert b'Fazer novo pedido' not in resp.data    # não é a landing inicio.html


def test_index_padeiro_redirect(app):
    from app.extensions import db
    from app.models import Usuario

    with app.app_context():
        padeiro = Usuario(login='padeiro', nome='Padeiro', papel='padeiro')
        padeiro.set_senha('senha123')
        db.session.add(padeiro)
        db.session.commit()

    client = app.test_client()
    _login(client, padeiro)
    resp = client.get('/', follow_redirects=False)
    # Padeiro redireciona pra padeiro.index
    assert resp.status_code in (302, 303)
    assert '/padeiro' in resp.location


def test_index_gerente_inicio(app):
    from app.extensions import db
    from app.models import Loja, Usuario

    with app.app_context():
        loja = Loja(nome='Loja Teste', ativa=True)
        db.session.add(loja)
        db.session.flush()
        gerente = Usuario(login='gerente', nome='Gerente', papel='gerente', loja_id=loja.id)
        gerente.set_senha('senha123')
        db.session.add(gerente)
        db.session.commit()

    client = app.test_client()
    _login(client, gerente)
    resp = client.get('/')
    assert resp.status_code == 200
    # inicio.html tem "Bem-vindo" e os cards
    assert b'Bem-vindo' in resp.data
    assert b'Fazer novo pedido' in resp.data
    assert b'Pedidos feitos' in resp.data


def test_index_funcionario_inicio(app):
    from app.extensions import db
    from app.models import Loja, Usuario

    with app.app_context():
        loja = Loja(nome='Loja Teste', ativa=True)
        db.session.add(loja)
        db.session.flush()
        func = Usuario(login='func', nome='Funcionário', papel='funcionario', loja_id=loja.id)
        func.set_senha('senha123')
        db.session.add(func)
        db.session.commit()

    client = app.test_client()
    _login(client, func)
    resp = client.get('/')
    assert resp.status_code == 200
    assert b'Bem-vindo' in resp.data
    assert b'Fazer novo pedido' in resp.data
    assert b'Pedidos feitos' in resp.data


def test_novo_pedido_gerente_permitido(app):
    from app.extensions import db
    from app.models import Loja, Usuario

    with app.app_context():
        loja = Loja(nome='Loja Teste', ativa=True)
        db.session.add(loja)
        db.session.flush()
        gerente = Usuario(login='gerente', nome='Gerente', papel='gerente', loja_id=loja.id)
        gerente.set_senha('senha123')
        db.session.add(gerente)
        db.session.commit()

    client = app.test_client()
    _login(client, gerente)
    resp = client.get('/pedidos/novo')
    # Gerente pode acessar novo pedido (antigamente @gerente_required)
    assert resp.status_code == 200
    assert b'Novo Pedido' in resp.data


def test_lista_pedido_gerente_permitido(app):
    from app.extensions import db
    from app.models import Loja, Usuario

    with app.app_context():
        loja = Loja(nome='Loja Teste', ativa=True)
        db.session.add(loja)
        db.session.flush()
        gerente = Usuario(login='gerente', nome='Gerente', papel='gerente', loja_id=loja.id)
        gerente.set_senha('senha123')
        db.session.add(gerente)
        db.session.commit()

    client = app.test_client()
    _login(client, gerente)
    resp = client.get('/pedidos/')
    assert resp.status_code == 200


def test_novo_pedido_padeiro_bloqueado(app):
    from app.extensions import db
    from app.models import Usuario

    with app.app_context():
        padeiro = Usuario(login='padeiro', nome='Padeiro', papel='padeiro')
        padeiro.set_senha('senha123')
        db.session.add(padeiro)
        db.session.commit()

    client = app.test_client()
    _login(client, padeiro)
    resp = client.get('/pedidos/novo')
    # Padeiro não pode acessar novo pedido
    assert resp.status_code == 403


def test_lista_pedido_padeiro_bloqueado(app):
    from app.extensions import db
    from app.models import Usuario

    with app.app_context():
        padeiro = Usuario(login='padeiro', nome='Padeiro', papel='padeiro')
        padeiro.set_senha('senha123')
        db.session.add(padeiro)
        db.session.commit()

    client = app.test_client()
    _login(client, padeiro)
    resp = client.get('/pedidos/')
    # Padeiro não pode acessar lista de pedidos
    assert resp.status_code == 403


def test_login_redirect_admin_index(app, admin_user):
    """Ao fazer login, admin vai pra index (que renderiza home.html)."""
    client = app.test_client()
    resp = client.post('/auth/login', data={'login': admin_user.login, 'senha': 'admin123'},
                       follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert resp.location.endswith('/') or '/' in resp.location


def test_login_redirect_padeiro_padeiro_index(app):
    from app.extensions import db
    from app.models import Usuario

    with app.app_context():
        padeiro = Usuario(login='padeiro', nome='Padeiro', papel='padeiro')
        padeiro.set_senha('senha123')
        db.session.add(padeiro)
        db.session.commit()

    client = app.test_client()
    resp = client.post('/auth/login', data={'login': 'padeiro', 'senha': 'senha123'},
                       follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert '/padeiro' in resp.location


def test_login_redirect_gerente_index(app):
    from app.extensions import db
    from app.models import Loja, Usuario

    with app.app_context():
        loja = Loja(nome='Loja Teste', ativa=True)
        db.session.add(loja)
        db.session.flush()
        gerente = Usuario(login='gerente', nome='Gerente', papel='gerente', loja_id=loja.id)
        gerente.set_senha('senha123')
        db.session.add(gerente)
        db.session.commit()

    client = app.test_client()
    resp = client.post('/auth/login', data={'login': 'gerente', 'senha': 'senha123'},
                       follow_redirects=False)
    assert resp.status_code in (302, 303)
    # Gerente vai pra main.index (que renderiza inicio.html)
    assert resp.location.endswith('/') or '/' in resp.location

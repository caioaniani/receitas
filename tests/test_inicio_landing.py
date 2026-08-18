"""Tela inicial didática para usuários não-padeiro.

Cobre: roteamento por papel (admin→home.html, não-padeiro→inicio.html),
cards "Fazer novo pedido" e "Pedidos feitos", e decorator @pedidos_required
nas rotas de pedido (novo/lista/buscar).
"""


def _login(client, user):
    """Aceita um id (int) ou uma instância Usuario atachada."""
    uid = user if isinstance(user, int) else user.id
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True


def _criar_usuario(app, login, papel, com_loja=False):
    """Cria um Usuario (e opcionalmente uma loja vinculada). Retorna o id
    capturado DENTRO do contexto — evita DetachedInstanceError ao usar depois."""
    from app.extensions import db
    from app.models import Loja, Usuario
    with app.app_context():
        loja_id = None
        if com_loja:
            loja = Loja(nome=f'Loja {login}', ativa=True)
            db.session.add(loja)
            db.session.flush()
            loja_id = loja.id
        u = Usuario(login=login, nome=login.capitalize(), papel=papel, loja_id=loja_id)
        u.set_senha('senha123')
        db.session.add(u)
        db.session.commit()
        return u.id


def test_index_admin_home(app, admin_user):
    """Admin vê home.html: o menu de cards dos menus principais (não mais o
    hero do copilot, que foi removido)."""
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
    client = app.test_client()
    _login(client, admin_user.id)
    resp = client.get('/')
    assert resp.status_code == 200
    assert b'menu-card' in resp.data               # marcador estável do home.html
    assert b'home-copilot-form' not in resp.data    # copilot web removido
    assert 'Escolha uma área para continuar'.encode() in resp.data


def test_index_padeiro_redirect(app):
    uid = _criar_usuario(app, 'padeiro', 'padeiro')
    client = app.test_client()
    _login(client, uid)
    resp = client.get('/', follow_redirects=False)
    # Padeiro redireciona pra padeiro.index
    assert resp.status_code in (302, 303)
    assert '/padeiro' in resp.location


def test_index_gerente_inicio(app):
    # Gerente SEM loja própria: ainda cai na landing.
    uid = _criar_usuario(app, 'gerente', 'gerente')
    client = app.test_client()
    _login(client, uid)
    resp = client.get('/')
    assert resp.status_code == 200
    # inicio.html tem "Bem-vindo" e os cards
    assert b'Bem-vindo' in resp.data
    assert b'Fazer novo pedido' in resp.data
    assert b'Pedidos feitos' in resp.data


def test_index_funcionario_inicio(app):
    uid = _criar_usuario(app, 'func', 'funcionario', com_loja=True)
    client = app.test_client()
    _login(client, uid)
    resp = client.get('/')
    assert resp.status_code == 200
    assert b'Bem-vindo' in resp.data
    assert b'Fazer novo pedido' in resp.data
    assert b'Pedidos feitos' in resp.data


def test_novo_pedido_gerente_permitido(app):
    # Gerente SEM loja própria precisa abrir o form e escolher a loja (fix do guard).
    uid = _criar_usuario(app, 'gerente', 'gerente')
    client = app.test_client()
    _login(client, uid)
    resp = client.get('/pedidos/novo')
    assert resp.status_code == 200
    assert b'Novo Pedido' in resp.data


def test_lista_pedido_gerente_permitido(app):
    uid = _criar_usuario(app, 'gerente', 'gerente')
    client = app.test_client()
    _login(client, uid)
    resp = client.get('/pedidos/')
    assert resp.status_code == 200


def test_novo_pedido_padeiro_bloqueado(app):
    uid = _criar_usuario(app, 'padeiro', 'padeiro')
    client = app.test_client()
    _login(client, uid)
    resp = client.get('/pedidos/novo')
    # Padeiro não pode acessar novo pedido
    assert resp.status_code == 403


def test_lista_pedido_padeiro_bloqueado(app):
    uid = _criar_usuario(app, 'padeiro', 'padeiro')
    client = app.test_client()
    _login(client, uid)
    resp = client.get('/pedidos/')
    # Padeiro não pode acessar lista de pedidos
    assert resp.status_code == 403


def test_login_redirect_admin_index(app, admin_user):
    """Ao fazer login, admin vai pra index (que renderiza home.html)."""
    client = app.test_client()
    resp = client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                       follow_redirects=False)
    assert resp.status_code in (302, 303)
    # admin cai no index (que renderiza home.html), não em padeiro nem minhas-fichas
    assert '/padeiro' not in resp.location
    assert 'minhas-fichas' not in resp.location


def test_login_redirect_padeiro_padeiro_index(app):
    _criar_usuario(app, 'padeiro', 'padeiro')
    client = app.test_client()
    resp = client.post('/auth/login', data={'login': 'padeiro', 'senha': 'senha123'},
                       follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert '/padeiro' in resp.location


def test_login_redirect_gerente_index(app):
    _criar_usuario(app, 'gerente', 'gerente')
    client = app.test_client()
    resp = client.post('/auth/login', data={'login': 'gerente', 'senha': 'senha123'},
                       follow_redirects=False)
    assert resp.status_code in (302, 303)
    # Gerente vai pra main.index (que renderiza inicio.html), não pra padeiro/minhas-fichas
    assert '/padeiro' not in resp.location
    assert 'minhas-fichas' not in resp.location

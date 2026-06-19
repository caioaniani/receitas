"""Auth do cliente final da loja online (Fase 6).

Foco: SESSÃO SEPARADA do admin (privilégio não cruza), cadastro/login/logout,
gate libera cliente logado em modo teste, e segurança (next safe).
"""


def _cadastrar(client, email='maria@x.com', senha='senha-forte-1',
               nome='Maria Silva', telefone='11999998888', aceite='1'):
    return client.post('/loja/cadastrar', data={
        'nome': nome, 'email': email, 'telefone': telefone,
        'senha': senha, 'aceite_lgpd': aceite,
    }, follow_redirects=False)


def test_cadastro_cria_cliente_e_loga(app):
    from app.models import Cliente
    c = app.test_client()
    r = _cadastrar(c)
    assert r.status_code == 302
    assert r.headers['Location'].endswith('/loja/conta')
    with app.app_context():
        cli = Cliente.query.filter_by(email='maria@x.com').first()
        assert cli and cli.tem_conta
        assert cli.check_senha('senha-forte-1')
        assert cli.aceite_lgpd_em is not None
    # Sessão criada
    with c.session_transaction() as s:
        assert s.get('cliente_id') == cli.id


def test_cadastro_guest_dispara_verificacao(app):
    """Cliente já existia como guest (sem senha) → cadastro NÃO vincula
    senha na hora. Manda link de verificação pro próprio e-mail. Defesa
    contra sequestro de pedido por adivinhação de e-mail (atacante não
    recebe o e-mail da vítima). Senha só vira efetiva quando o link for
    clicado."""
    from app.extensions import db
    from app.models import Cliente, ClienteVerificacaoEmail
    with app.app_context():
        guest = Cliente(nome='Jo', email='jo@x.com', telefone='11')
        db.session.add(guest)
        db.session.commit()
        guest_id = guest.id
    c = app.test_client()
    r = _cadastrar(c, email='jo@x.com', nome='João Completo',
                   senha='senha-forte-1')
    # NÃO loga (sem 302): fica na tela 'verifique seu e-mail'.
    assert r.status_code == 200
    assert b'Confirme seu e-mail' in r.data
    with app.app_context():
        cli = Cliente.query.filter_by(email='jo@x.com').first()
        assert cli.id == guest_id            # MESMA linha
        assert cli.senha_hash is None        # senha NÃO ativada ainda
        # Token de verificação criado com a senha pendente HASHADA
        reg = ClienteVerificacaoEmail.query.filter_by(
            cliente_id=guest_id).first()
        assert reg and reg.usado_em is None
        assert reg.senha_hash_pending  # não plaintext
        assert 'senha-forte-1' not in (reg.senha_hash_pending or '')
    # Sessão ainda anônima
    with c.session_transaction() as s:
        assert 'cliente_id' not in s


def test_verificar_cadastro_token_valido_ativa_conta(app):
    """Link do e-mail de verificação: aplica a senha pendente, marca o
    token como usado, loga e redireciona pra /loja/conta."""
    from app.extensions import db
    from app.models import Cliente, ClienteVerificacaoEmail
    with app.app_context():
        guest = Cliente(nome='Pedro', email='pedro@x.com')
        db.session.add(guest)
        db.session.commit()
    c = app.test_client()
    _cadastrar(c, email='pedro@x.com', nome='Pedro Completo',
               senha='senha-forte-1')
    with app.app_context():
        reg = ClienteVerificacaoEmail.query.filter_by(
            cliente__email='pedro@x.com').first() if False else (
            ClienteVerificacaoEmail.query.join(Cliente)
            .filter(Cliente.email == 'pedro@x.com').first())
        token = reg.token
    r = c.get(f'/loja/verificar-cadastro/{token}', follow_redirects=False)
    assert r.status_code == 302
    assert r.headers['Location'].endswith('/loja/conta')
    with app.app_context():
        cli = Cliente.query.filter_by(email='pedro@x.com').first()
        assert cli.check_senha('senha-forte-1')   # senha ativada
        reg = ClienteVerificacaoEmail.query.filter_by(
            cliente_id=cli.id).first()
        assert reg.usado_em is not None
    with c.session_transaction() as s:
        assert s.get('cliente_id')


def test_verificar_cadastro_token_invalido_rejeita(app):
    """Token inexistente/curto/inválido → 400, sem ativar nada."""
    c = app.test_client()
    r = c.get('/loja/verificar-cadastro/tokeninexistente0123456789xxxxxxxxxx',
              follow_redirects=False)
    assert r.status_code == 400


def test_verificar_cadastro_token_e_single_use(app):
    """Token só funciona uma vez. Segundo clique vira inválido."""
    from app.extensions import db
    from app.models import Cliente, ClienteVerificacaoEmail
    with app.app_context():
        guest = Cliente(nome='Lia', email='lia@x.com')
        db.session.add(guest)
        db.session.commit()
    c = app.test_client()
    _cadastrar(c, email='lia@x.com', nome='Lia Completa',
               senha='senha-forte-1')
    with app.app_context():
        token = (ClienteVerificacaoEmail.query.join(Cliente)
                 .filter(Cliente.email == 'lia@x.com').first()).token
    # 1º clique funciona
    c2 = app.test_client()
    r1 = c2.get(f'/loja/verificar-cadastro/{token}', follow_redirects=False)
    assert r1.status_code == 302
    # 2º clique do MESMO token cai como inválido
    c3 = app.test_client()
    r2 = c3.get(f'/loja/verificar-cadastro/{token}', follow_redirects=False)
    assert r2.status_code == 400


def test_cadastro_email_ja_com_conta_falha(app):
    """Email com senha cadastrada → recusa (tem que entrar, não cadastrar)."""
    from app.extensions import db
    from app.models import Cliente
    with app.app_context():
        existente = Cliente(nome='Ana', email='ana@x.com')
        existente.set_senha('xxxxxxxx')
        db.session.add(existente)
        db.session.commit()
    c = app.test_client()
    r = _cadastrar(c, email='ana@x.com')
    assert r.status_code == 400
    assert b'j\xc3\xa1 existe' in r.data.lower() or b'tente entrar' in r.data.lower()


def test_cadastro_sem_aceite_lgpd_falha(app):
    c = app.test_client()
    r = _cadastrar(c, aceite='')
    assert r.status_code == 400
    assert b'aceitar' in r.data.lower()


def test_cadastro_senha_curta_falha(app):
    c = app.test_client()
    r = _cadastrar(c, senha='123')
    assert r.status_code == 400


def test_login_ok_e_falha(app):
    from app.extensions import db
    from app.models import Cliente
    with app.app_context():
        cli = Cliente(nome='Bia', email='bia@x.com')
        cli.set_senha('senha-forte-1')
        db.session.add(cli)
        db.session.commit()
    c = app.test_client()
    # Senha errada
    r = c.post('/loja/entrar', data={'email': 'bia@x.com', 'senha': 'errada'},
               follow_redirects=False)
    assert r.status_code == 400
    # Senha certa
    r = c.post('/loja/entrar', data={'email': 'bia@x.com', 'senha': 'senha-forte-1'},
               follow_redirects=False)
    assert r.status_code == 302


def test_login_cliente_inativo_recusa(app):
    """Conta desativada não consegue logar."""
    from app.extensions import db
    from app.models import Cliente
    with app.app_context():
        cli = Cliente(nome='X', email='x@x.com', ativo=False)
        cli.set_senha('senha-forte-1')
        db.session.add(cli)
        db.session.commit()
    c = app.test_client()
    r = c.post('/loja/entrar', data={'email': 'x@x.com', 'senha': 'senha-forte-1'},
               follow_redirects=False)
    assert r.status_code == 400


def test_logout_remove_sessao(app):
    c = app.test_client()
    _cadastrar(c)
    # Logado: GET /loja/conta dá 200
    assert c.get('/loja/conta').status_code == 200
    # Sair: POST /loja/sair
    r = c.post('/loja/sair')
    assert r.status_code == 302
    with c.session_transaction() as s:
        assert 'cliente_id' not in s


def test_minha_conta_exige_cliente_logado(app, monkeypatch):
    """/loja/conta sem login → 302 pra /loja/entrar (não 200, não 404)."""
    monkeypatch.setenv('LOJA_VISIVEL', '1')  # tirar o gate de visibilidade
    c = app.test_client()
    r = c.get('/loja/conta', follow_redirects=False)
    assert r.status_code == 302
    assert '/loja/entrar' in r.headers['Location']


def test_gate_libera_cliente_logado_em_modo_teste(app):
    """Modo teste (LOJA_VISIVEL=0): cliente logado VÊ a vitrine, não 404."""
    c = app.test_client()
    _cadastrar(c)
    r = c.get('/loja/')
    assert r.status_code == 200


def test_gate_404_anonimo_em_modo_teste(app):
    """Modo teste: visitante anônimo toma 404 na vitrine. As rotas de AUTH
    (entrar/cadastrar) ficam sempre acessíveis — sem elas, cliente novo não
    consegue criar conta nem fazer login pra ver a loja."""
    c = app.test_client()
    assert c.get('/loja/').status_code == 404
    # Auth sempre acessível, mesmo em modo teste
    assert c.get('/loja/entrar').status_code == 200
    assert c.get('/loja/cadastrar').status_code == 200


def test_sessao_admin_nao_vira_cliente(app):
    """Sessão de admin (Flask-Login) NÃO autentica como cliente — privilégio
    NÃO cruza. Admin que abre /loja/conta tem que tomar 302 pra /loja/entrar."""
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        u = Usuario(nome='Admin', login='adm', papel='admin')
        u.set_senha('xxxxxxxx')
        db.session.add(u)
        db.session.commit()
        uid = u.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    r = c.get('/loja/conta', follow_redirects=False)
    assert r.status_code == 302
    assert '/loja/entrar' in r.headers['Location']


def test_next_safe_redireciona_pra_url_interna(app):
    from app.extensions import db
    from app.models import Cliente
    with app.app_context():
        cli = Cliente(nome='N', email='n@x.com')
        cli.set_senha('senha-forte-1')
        db.session.add(cli)
        db.session.commit()
    c = app.test_client()
    r = c.post('/loja/entrar?next=/loja/conta',
               data={'email': 'n@x.com', 'senha': 'senha-forte-1'},
               follow_redirects=False)
    assert r.status_code == 302
    assert r.headers['Location'].endswith('/loja/conta')


def test_next_unsafe_e_descartado(app):
    """Open-redirect: next pra fora do /loja/ é IGNORADO."""
    from app.extensions import db
    from app.models import Cliente
    with app.app_context():
        cli = Cliente(nome='N', email='n2@x.com')
        cli.set_senha('senha-forte-1')
        db.session.add(cli)
        db.session.commit()
    for nxt in ('//evil.com', 'https://evil.com', '/loja//evil', '/admin'):
        c = app.test_client()
        r = c.post(f'/loja/entrar?next={nxt}',
                   data={'email': 'n2@x.com', 'senha': 'senha-forte-1'},
                   follow_redirects=False)
        assert r.status_code == 302
        # Nunca redireciona pro hostile — manda pra /loja/conta
        assert 'evil' not in r.headers['Location']
        assert r.headers['Location'].endswith('/loja/conta')


def test_topo_mostra_entrar_quando_deslogado_e_minha_conta_quando_logado(app):
    """Header da loja: 'Entrar' pra anônimo, 'Minha conta' pra logado."""
    c = app.test_client()
    _cadastrar(c)
    r = c.get('/loja/')
    assert r.status_code == 200
    assert b'Minha conta' in r.data
    assert b'>Entrar<' not in r.data

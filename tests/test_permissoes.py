"""Permissoes operacionais editaveis por papel (web + copilot + Slack).

Os padroes do codigo espelham o comportamento legado; a tabela PermissaoPapel
sobrepoe. admin/owner sempre full (nao entram na matriz).
"""


def _login(client, user):
    uid = user if isinstance(user, int) else user.id
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True


def test_defaults_espelham_comportamento_atual(app):
    from app.services import permissoes
    with app.app_context():
        permissoes.invalidar()
        # nivel gerente (copilot)
        assert permissoes.pode('gerente', 'criar_pedido') is True
        assert permissoes.pode('funcionario', 'criar_pedido') is False
        assert permissoes.pode('producao', 'criar_pedido') is False
        # nivel funcionario (todos os editaveis)
        assert permissoes.pode('funcionario', 'consultar_pedido') is True
        assert permissoes.pode('padeiro', 'registrar_desperdicio') is True
        # telas web
        assert permissoes.pode('gerente', 'web_estoque_loja') is True
        assert permissoes.pode('producao', 'web_estoque_loja') is False
        assert permissoes.pode('producao', 'web_producao') is True
        assert permissoes.pode('rh', 'web_rh') is True
        assert permissoes.pode('padeiro', 'web_pedidos') is False
        assert permissoes.pode('funcionario', 'web_pedidos') is True
        # capacidade desconhecida = nega
        assert permissoes.pode('gerente', 'inexistente') is False


def test_override_liga_e_desliga(app):
    from app.extensions import db
    from app.models import PermissaoPapel
    from app.services import permissoes
    with app.app_context():
        assert permissoes.pode('funcionario', 'criar_pedido') is False
        db.session.add(PermissaoPapel(papel='funcionario', capacidade='criar_pedido', permitido=True))
        db.session.commit()
        permissoes.invalidar()
        assert permissoes.pode('funcionario', 'criar_pedido') is True
        # desligar um default
        db.session.add(PermissaoPapel(papel='gerente', capacidade='criar_pedido', permitido=False))
        db.session.commit()
        permissoes.invalidar()
        assert permissoes.pode('gerente', 'criar_pedido') is False


def test_copilot_pode_usar_respeita_override(app):
    from app.extensions import db
    from app.models import PermissaoPapel, Usuario
    from app.services import copilot, permissoes
    with app.app_context():
        func = Usuario(login='f1', nome='F', papel='funcionario')
        func.set_senha('x')
        db.session.add(func)
        db.session.commit()
        permissoes.invalidar()
        assert copilot.pode_usar('criar_pedido', func) is False
        db.session.add(PermissaoPapel(papel='funcionario', capacidade='criar_pedido', permitido=True))
        db.session.commit()
        permissoes.invalidar()
        assert copilot.pode_usar('criar_pedido', func) is True


def test_admin_owner_sempre_full(app):
    from app.extensions import db
    from app.models import PermissaoPapel, Usuario
    from app.services import copilot, permissoes
    with app.app_context():
        adm = Usuario(login='a1', nome='A', papel='admin')
        adm.set_senha('x')
        owner = Usuario(login='ow', nome='O', papel='admin', is_owner=True)
        owner.set_senha('x')
        db.session.add_all([adm, owner])
        # tenta desligar pra gerente — nao afeta admin/owner
        db.session.add(PermissaoPapel(papel='gerente', capacidade='criar_pedido', permitido=False))
        db.session.commit()
        permissoes.invalidar()
        assert copilot.pode_usar('criar_pedido', adm) is True
        assert copilot.pode_usar('editar_pedido', adm) is True
        assert copilot.pode_usar('criar_pedido', owner) is True


def test_decorator_web_respeita_override(app):
    """funcionario nao acessa /pedidos/novo se web_pedidos for desligado p/ ele."""
    from app.extensions import db
    from app.models import Loja, PermissaoPapel, Usuario
    from app.services import permissoes
    with app.app_context():
        loja = Loja(nome='L', ativa=True)
        db.session.add(loja)
        db.session.flush()
        func = Usuario(login='f2', nome='F', papel='funcionario', loja_id=loja.id)
        func.set_senha('x')
        db.session.add(func)
        db.session.commit()
        fid = func.id
        permissoes.invalidar()
    client = app.test_client()
    _login(client, fid)
    # default: funcionario acessa /pedidos/ (web_pedidos inclui funcionario)
    assert client.get('/pedidos/').status_code == 200
    with app.app_context():
        db.session.add(PermissaoPapel(papel='funcionario', capacidade='web_pedidos', permitido=False))
        db.session.commit()
        permissoes.invalidar()
    assert client.get('/pedidos/').status_code == 403


def test_pagina_permissoes_gerente_403(app):
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        ger = Usuario(login='g1', nome='G', papel='gerente')
        ger.set_senha('x')
        db.session.add(ger)
        db.session.commit()
        gid = ger.id
    client = app.test_client()
    _login(client, gid)
    assert client.get('/admin/permissoes').status_code == 403


def test_pagina_permissoes_owner_200(app):
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        owner = Usuario(login='o1', nome='O', papel='admin', is_owner=True)
        owner.set_senha('x')
        db.session.add(owner)
        db.session.commit()
        oid = owner.id
    client = app.test_client()
    _login(client, oid)
    r = client.get('/admin/permissoes')
    assert r.status_code == 200
    assert b'Permiss' in r.data


def test_salvar_via_post(app):
    from app.extensions import db
    from app.models import Usuario
    from app.services import permissoes
    with app.app_context():
        owner = Usuario(login='o2', nome='O', papel='admin', is_owner=True)
        owner.set_senha('x')
        db.session.add(owner)
        db.session.commit()
        oid = owner.id
        # form = estado atual (defaults marcados) + liga criar_pedido p/ funcionario
        form = {}
        for linha in permissoes.estado_atual():
            for p, on in linha['estados'].items():
                if on:
                    form[f"{linha['chave']}__{p}"] = 'on'
        form['criar_pedido__funcionario'] = 'on'
    client = app.test_client()
    _login(client, oid)
    resp = client.post('/admin/permissoes', data=form, follow_redirects=False)
    assert resp.status_code in (302, 303)
    with app.app_context():
        permissoes.invalidar()
        assert permissoes.pode('funcionario', 'criar_pedido') is True
        # defaults preservados (nao viraram off)
        assert permissoes.pode('funcionario', 'consultar_pedido') is True
        assert permissoes.pode('gerente', 'criar_pedido') is True

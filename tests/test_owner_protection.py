"""Protecao do owner: admin nao-owner nao mexe no owner.

Regressao detectada na auditoria — antes, admin comum podia resetar
senha do owner via /auth/usuarios/<id>/reset-senha e logar como ele.
"""
import pytest


@pytest.fixture
def cliente(app):
    return app.test_client()


@pytest.fixture
def owner(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Dono', login='dono', papel='admin', is_owner=True)
    u.set_senha('senha-do-dono')
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def outro_admin(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Admin2', login='admin2', papel='admin', is_owner=False)
    u.set_senha('senha-admin2')
    db.session.add(u)
    db.session.commit()
    return u


def _login(cliente, login_val, senha):
    return cliente.post('/auth/login',
                         data={'login': login_val, 'senha': senha})


# ─── reset_senha ───────────────────────────────────────────────────────

def test_admin_nao_owner_nao_reseta_senha_do_owner(cliente, owner, outro_admin):
    """O bug original — admin comum trocando senha do owner pra logar como ele."""
    _login(cliente, 'admin2', 'senha-admin2')
    r = cliente.post(f'/auth/usuarios/{owner.id}/reset-senha',
                      data={'nova_senha': 'senha-hackeada'},
                      follow_redirects=False)
    assert r.status_code == 302  # redirect com flash de erro

    # E o mais importante: senha do owner NAO mudou
    from app.extensions import db
    db.session.expire_all()
    from app.models import Usuario
    o = Usuario.query.get(owner.id)
    assert o.check_senha('senha-do-dono'), 'senha do owner foi resetada por admin nao-owner!'
    assert not o.check_senha('senha-hackeada')


def test_owner_reseta_senha_de_outro_admin_ok(cliente, owner, outro_admin):
    """Owner pode tudo. Reset de senha de outro admin funciona."""
    _login(cliente, 'dono', 'senha-do-dono')
    r = cliente.post(f'/auth/usuarios/{outro_admin.id}/reset-senha',
                      data={'nova_senha': 'nova-senha-admin2'},
                      follow_redirects=False)
    assert r.status_code == 302
    from app.extensions import db
    db.session.expire_all()
    from app.models import Usuario
    a = Usuario.query.get(outro_admin.id)
    assert a.check_senha('nova-senha-admin2')


def test_reset_senha_aceita_minimo_8_chars(cliente, owner, outro_admin):
    """Defesa em profundidade: rota rejeita senha curta."""
    _login(cliente, 'dono', 'senha-do-dono')
    r = cliente.post(f'/auth/usuarios/{outro_admin.id}/reset-senha',
                      data={'nova_senha': '123'},
                      follow_redirects=False)
    assert r.status_code == 302
    from app.extensions import db
    db.session.expire_all()
    from app.models import Usuario
    a = Usuario.query.get(outro_admin.id)
    assert not a.check_senha('123'), 'senha curta foi aceita!'


# ─── excluir_usuario ───────────────────────────────────────────────────

def test_admin_nao_owner_nao_exclui_owner(cliente, owner, outro_admin):
    _login(cliente, 'admin2', 'senha-admin2')
    r = cliente.post(f'/auth/usuarios/{owner.id}/excluir', follow_redirects=False)
    assert r.status_code == 302
    from app.extensions import db
    db.session.expire_all()
    from app.models import Usuario
    o = Usuario.query.get(owner.id)
    assert o is not None, 'owner foi excluido por admin nao-owner!'


def test_admin_nao_pode_excluir_a_si_mesmo(cliente, outro_admin):
    _login(cliente, 'admin2', 'senha-admin2')
    r = cliente.post(f'/auth/usuarios/{outro_admin.id}/excluir', follow_redirects=False)
    assert r.status_code == 302
    from app.extensions import db
    db.session.expire_all()
    from app.models import Usuario
    a = Usuario.query.get(outro_admin.id)
    assert a is not None, 'admin se excluiu sozinho'


# ─── alterar_papel (ja existia, mas vale teste) ────────────────────────

def test_owner_nao_pode_ter_papel_alterado(cliente, owner):
    """Mesmo owner logado nao pode rebaixar a si mesmo via /alterar-papel."""
    _login(cliente, 'dono', 'senha-do-dono')
    r = cliente.post(f'/auth/usuarios/{owner.id}/papel',
                      data={'papel': 'funcionario'},
                      follow_redirects=False)
    assert r.status_code == 302
    from app.extensions import db
    db.session.expire_all()
    from app.models import Usuario
    o = Usuario.query.get(owner.id)
    assert o.papel == 'admin', 'owner foi rebaixado'

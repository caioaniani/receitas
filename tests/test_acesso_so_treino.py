"""Senha provisória forçada no 1º login + acesso "só treinamento" por pessoa
(23/07/2026, decisão do dono). Gate global em `before_request`."""
from app.extensions import db
from app.models import Usuario


def _mk(login, **kw):
    u = Usuario(nome='X', login=login, papel=kw.pop('papel', 'funcionario'), **kw)
    u.set_senha('senha-atual-1')
    db.session.add(u)
    db.session.commit()
    return u


def _cli(app, uid):
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    return c


# ── Senha provisória ──────────────────────────────────────────────────────

def test_senha_provisoria_forca_troca(app):
    with app.app_context():
        uid = _mk('prov', senha_provisoria=True).id
    c = _cli(app, uid)
    r = c.get('/', follow_redirects=False)
    assert r.status_code == 302 and '/auth/minha-senha' in r.headers['Location']
    assert c.get('/auth/minha-senha').status_code == 200   # a troca em si abre


def test_troca_de_senha_libera_o_gate(app):
    with app.app_context():
        uid = _mk('prov2', senha_provisoria=True).id
    c = _cli(app, uid)
    r = c.post('/auth/minha-senha', data={
        'senha_atual': 'senha-atual-1', 'nova_senha': 'nova-senha-9',
        'confirma_senha': 'nova-senha-9'}, follow_redirects=False)
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(Usuario, uid).senha_provisoria is False
    r2 = c.get('/', follow_redirects=False)
    assert '/auth/minha-senha' not in (r2.headers.get('Location') or '')


# ── Só treinamento ────────────────────────────────────────────────────────

def test_so_treino_redireciona_pra_treino(app):
    with app.app_context():
        uid = _mk('sot', somente_treino=True).id
    c = _cli(app, uid)
    r = c.get('/', follow_redirects=False)
    assert r.status_code == 302 and '/treino' in r.headers['Location']
    assert c.get('/treino/').status_code == 200            # o treino abre


def test_so_treino_tem_atalho_na_sidebar_v2(app):
    """A conta restrita não pode ficar presa na aula sem caminho de volta."""
    with app.app_context():
        uid = _mk('sot-menu', somente_treino=True).id
    c = _cli(app, uid)

    html = c.get('/treino/').get_data(as_text=True)

    assert 'ui-v2-sidebar' in html
    assert 'href="/treino/"' in html
    assert '<span>Treinamento</span>' in html
    assert 'bi-mortarboard' in html


def test_so_treino_barra_url_direta_de_outra_area(app):
    with app.app_context():
        uid = _mk('sot2', somente_treino=True).id
    c = _cli(app, uid)
    # URL de outra área (admin): o gate volta pro treino ANTES do @admin_required
    r = c.get('/auth/usuarios', follow_redirects=False)
    assert r.status_code == 302 and '/treino' in r.headers['Location']


def test_conta_normal_nao_e_afetada(app, admin_user):
    """Usuário normal (sem flags) navega livre — o gate é no-op."""
    c = _cli(app, admin_user.id)
    assert c.get('/auth/usuarios', follow_redirects=False).status_code == 200


# ── UI admin: criar com flag + toggle ─────────────────────────────────────

def test_novo_usuario_so_treino_marca_flags(app, admin_user):
    c = _cli(app, admin_user.id)
    c.post('/auth/usuarios/novo', data={
        'nome': 'Só Treino', 'login': 'sotreino@x.com', 'email': '',
        'papel': 'funcionario', 'somente_treino': '1'})
    with app.app_context():
        u = Usuario.query.filter_by(login='sotreino@x.com').first()
        assert u is not None
        assert u.somente_treino is True and u.senha_provisoria is True


def test_toggle_somente_treino(app, admin_user):
    with app.app_context():
        uid = _mk('tog', somente_treino=False).id
    c = _cli(app, admin_user.id)
    c.post(f'/auth/usuarios/{uid}/somente-treino')
    with app.app_context():
        assert db.session.get(Usuario, uid).somente_treino is True
    c.post(f'/auth/usuarios/{uid}/somente-treino')          # desliga
    with app.app_context():
        assert db.session.get(Usuario, uid).somente_treino is False


def test_toggle_recusa_a_propria_conta(app, admin_user):
    """Admin não pode marcar A SI MESMO como só-treino (auto-lockout)."""
    c = _cli(app, admin_user.id)
    c.post(f'/auth/usuarios/{admin_user.id}/somente-treino')
    with app.app_context():
        assert db.session.get(Usuario, admin_user.id).somente_treino is False


def test_troca_forcada_recusa_mesma_senha(app):
    """Na troca forçada, repetir a senha provisória do e-mail não vale."""
    with app.app_context():
        uid = _mk('mesma', senha_provisoria=True).id
    c = _cli(app, uid)
    r = c.post('/auth/minha-senha', data={
        'senha_atual': 'senha-atual-1', 'nova_senha': 'senha-atual-1',
        'confirma_senha': 'senha-atual-1'}, follow_redirects=False)
    assert r.status_code == 302 and '/auth/minha-senha' in r.headers['Location']
    with app.app_context():
        assert db.session.get(Usuario, uid).senha_provisoria is True   # não trocou


def test_reset_senha_remarca_provisoria(app, admin_user):
    with app.app_context():
        uid = _mk('rst', senha_provisoria=False).id
    c = _cli(app, admin_user.id)
    c.post(f'/auth/usuarios/{uid}/reset-senha', data={'nova_senha': 'trocada-8x'})
    with app.app_context():
        assert db.session.get(Usuario, uid).senha_provisoria is True

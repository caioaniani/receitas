"""@admin_required e demais decorators de papel.

Pega regressao em: funcionario acessando rotas de admin, decorator
permissivo demais, ou 403 substituido por flash+redirect (mudanca de
comportamento).
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
    u.set_senha('joao123')
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def gerente(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Maria', login='maria', papel='gerente')
    u.set_senha('maria123')
    db.session.add(u)
    db.session.commit()
    return u


def _login(cliente, login_val, senha):
    return cliente.post('/auth/login',
                         data={'login': login_val, 'senha': senha},
                         follow_redirects=False)


# ─── /usuarios — admin only ────────────────────────────────────────────

def test_usuarios_sem_login_redireciona(cliente):
    """Anonimo na /usuarios vai pra login (Flask-Login)."""
    r = cliente.get('/usuarios', follow_redirects=False)
    assert r.status_code in (301, 302, 308)
    assert '/auth/login' in r.headers.get('Location', '')


def test_usuarios_funcionario_403(cliente, funcionario):
    """Funcionario logado nao pode ver /usuarios (403)."""
    _login(cliente, 'joao', 'joao123')
    r = cliente.get('/usuarios', follow_redirects=False)
    assert r.status_code == 403


def test_usuarios_gerente_403(cliente, gerente):
    """Gerente nao eh admin — /usuarios bloqueia."""
    _login(cliente, 'maria', 'maria123')
    r = cliente.get('/usuarios', follow_redirects=False)
    assert r.status_code == 403


def test_usuarios_admin_ok(cliente, admin_user):
    """Admin acessa /usuarios normal."""
    _login(cliente, 'admin', '123')
    r = cliente.get('/usuarios', follow_redirects=False)
    assert r.status_code == 200


def test_novo_usuario_funcionario_403(cliente, funcionario):
    """POST de criar usuario bloqueado pra funcionario."""
    _login(cliente, 'joao', 'joao123')
    r = cliente.post('/usuarios/novo',
                      data={'nome': 'X', 'login': 'x', 'senha': 'x',
                            'papel': 'funcionario'},
                      follow_redirects=False)
    assert r.status_code == 403
    # E nao criou
    from app.models import Usuario
    assert Usuario.query.filter_by(login='x').first() is None


def test_painel_funcionario_403(cliente, funcionario):
    _login(cliente, 'joao', 'joao123')
    r = cliente.get('/painel', follow_redirects=False)
    assert r.status_code == 403


# ─── concluir — caso misto (admin OU dono da atribuicao) ──────────────

def test_concluir_atribuicao_dono(cliente, funcionario):
    """Funcionario pode concluir SUA propria atribuicao."""
    from app.extensions import db
    from app.models import Atribuicao, Receita
    r = Receita(nome='X', categoria='Y', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.commit()
    a = Atribuicao(receita_id=r.id, usuario_id=funcionario.id)
    db.session.add(a)
    db.session.commit()
    aid = a.id

    _login(cliente, 'joao', 'joao123')
    resp = cliente.post(f'/atribuicao/{aid}/concluir', follow_redirects=False)
    assert resp.status_code == 302

    db.session.expire_all()
    a_atualizada = Atribuicao.query.get(aid)
    assert a_atualizada.status == 'concluida'


def test_concluir_atribuicao_de_outro_bloqueia(cliente, app, funcionario):
    """Funcionario tentando concluir atribuicao DE OUTRO usuario eh bloqueado."""
    from app.extensions import db
    from app.models import Atribuicao, Receita, Usuario
    outro = Usuario(nome='Pedro', login='pedro', papel='funcionario')
    outro.set_senha('p')
    r = Receita(nome='X', categoria='Y', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add_all([outro, r])
    db.session.commit()
    a = Atribuicao(receita_id=r.id, usuario_id=outro.id)
    db.session.add(a)
    db.session.commit()
    aid = a.id

    _login(cliente, 'joao', 'joao123')
    resp = cliente.post(f'/atribuicao/{aid}/concluir', follow_redirects=False)
    # Eh redirect pra minhas_fichas com flash de erro — nao status 200 sucesso
    assert resp.status_code == 302
    db.session.expire_all()
    a_atualizada = Atribuicao.query.get(aid)
    assert a_atualizada.status != 'concluida', 'Funcionario invadiu atribuicao alheia'

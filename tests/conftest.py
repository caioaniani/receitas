"""Fixtures de teste — SQLite in-memory + seed minimo.

Cada teste recebe um app/db isolado. Usar fixture `admin_user` pra ter
um Usuario admin pronto. Fixture `loja` cria uma loja operacional.
Fixture `catalogo` cria 1 receita + 1 produto + 1 MP.

Sem dependencia de Anthropic API: testes mockam tool_call ou chamam
diretamente os enrichers/executores.
"""
import os
import pytest


@pytest.fixture
def app():
    os.environ['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    os.environ['SECRET_KEY'] = 'test-secret'
    from app import create_app
    from app.extensions import db
    app = create_app()
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
    with app.app_context():
        db.drop_all()
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def _make_receita(nome, categoria='Paes'):
    """Cria Receita com defaults validos pra NOT NULLs."""
    from app.models import Receita
    return Receita(nome=nome, categoria=categoria, rendimento_qtd=1,
                   rendimento_unidade='un', peso_base=100.0)


@pytest.fixture
def admin_user(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='admin teste', login='admin', papel='admin')
    u.set_senha('123')
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def loja(app):
    from app.extensions import db
    from app.models import Loja
    l = Loja(nome='Ribeiro do Vale', ativa=True)
    db.session.add(l)
    db.session.commit()
    return l


@pytest.fixture
def catalogo(app):
    """Cria 1 receita, 1 produto, 1 MP pra testes que precisam de match."""
    from app.extensions import db
    from app.models import Receita, Produto, MateriaPrima
    r = _make_receita('Croissant Tradicional', categoria='Croissants')
    p = Produto(nome='Pao Frances', ativo=True)
    mp = MateriaPrima(nome='Farinha', unidade='kg', custo_por_kg=5.0)
    db.session.add_all([r, p, mp])
    db.session.commit()
    return {'receita': r, 'produto': p, 'mp': mp}

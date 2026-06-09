"""Fixtures de teste — app/schema criados 1x por sessao + reset por DELETE.

Cada teste recebe um banco LIMPO via fixture `app`. Usar fixture `admin_user`
pra ter um Usuario admin pronto. Fixture `loja` cria uma loja operacional.
Fixture `catalogo` cria 1 receita + 1 produto + 1 MP.

PERFORMANCE (refactor 2026-06-09): antes cada teste fazia `create_app()` +
`drop_all()` + `create_all()` (~1.16s/teste = ~9.4 min de setup nos 487
testes). Agora o app e o schema sao criados UMA vez por sessao, e o reset
entre testes vira `DELETE` das linhas de todas as tabelas (~0.02s/teste, 43x
mais rapido). DELETE (e nao drop+create) mantem o arquivo SQLite vivo, entao
codigo que abre `db.engine.connect()` proprio (seru_cron, blob_migrator)
continua enxergando os dados — sem a fragilidade do padrao de transacao.

Sem dependencia de Anthropic API: testes mockam tool_call ou chamam
diretamente os enrichers/executores.
"""
import os

import pytest

# ── Paralelizacao (pytest-xdist) ──────────────────────────────────────────
# Cada worker do xdist roda em processo separado. Pra eles nao brigarem pelo
# MESMO arquivo SQLite (config.py:8 le DATABASE_URL no import), damos a cada
# worker o seu proprio arquivo. Sem xdist (PYTEST_XDIST_WORKER ausente), nada
# muda — comportamento de antes. Tem que rodar ANTES de qualquer `from app
# import ...` (que importa config.py); por isso fica no topo do conftest.
_xdist_worker = os.environ.get('PYTEST_XDIST_WORKER')
if _xdist_worker:
    import tempfile
    os.environ['DATABASE_URL'] = (
        f'sqlite:///{tempfile.gettempdir()}/padaria_test_{_xdist_worker}.db')

os.environ.setdefault('SECRET_KEY', 'test-secret')
os.environ['PYTEST_RUNNING'] = '1'


@pytest.fixture(scope='session')
def _app_session():
    """Cria o app + schema UMA vez por sessao de testes."""
    from app import create_app
    from app.extensions import db
    application = create_app()
    application.config['TESTING'] = True
    application.config['WTF_CSRF_ENABLED'] = False
    with application.app_context():
        db.drop_all()
        db.create_all()
    return application


@pytest.fixture(scope='session')
def _config_baseline(_app_session):
    """Snapshot da config logo apos criar o app. Restaurada apos CADA teste
    pra mutacoes (`app.config['X']=Y`, comuns nos testes) nao vazarem entre
    testes, ja que o objeto `app` agora eh compartilhado pela sessao."""
    return dict(_app_session.config)


def _limpar_tabelas(db):
    """Apaga as linhas de todas as tabelas, em ordem reversa de FK (seguro
    mesmo com foreign_keys=ON). Substitui o drop+create por teste."""
    for table in reversed(db.metadata.sorted_tables):
        db.session.execute(table.delete())
    db.session.commit()


@pytest.fixture
def app(_app_session, _config_baseline):
    from app.extensions import db, limiter
    application = _app_session
    ctx = application.app_context()
    ctx.push()
    # Estado limpo garantido no INICIO do teste (mesmo se o anterior crashou).
    _limpar_tabelas(db)
    # Zera o rate limiter — antes cada teste tinha app novo (limiter zerado);
    # com app de sessao o estado acumula e estoura 429 (quebrava 91 testes).
    try:
        limiter.reset()
    except Exception:  # noqa: BLE001
        pass
    try:
        yield application
    finally:
        db.session.remove()
        ctx.pop()
        # Desfaz mutacoes de config feitas pelo teste (app compartilhado).
        application.config.clear()
        application.config.update(_config_baseline)


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
    from app.models import MateriaPrima, Produto
    r = _make_receita('Croissant Tradicional', categoria='Croissants')
    p = Produto(nome='Pao Frances', ativo=True)
    mp = MateriaPrima(nome='Farinha', unidade='kg', custo_por_kg=5.0)
    db.session.add_all([r, p, mp])
    db.session.commit()
    return {'receita': r, 'produto': p, 'mp': mp}

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

# ── Isolamento do banco POR PROCESSO de teste ─────────────────────────────
# Sem DATABASE_URL setado, config.py cai no ~/.padaria/padaria.db FIXO. Dois
# problemas reais quando isso vale pra todo processo pytest:
#   1. Duas invocacoes pytest concorrentes (ou os workers do xdist) batem no
#      MESMO arquivo. Como o reset entre testes eh DELETE de todas as linhas +
#      recriacao do admin no startup, os processos apagam/recriam as linhas uns
#      dos outros no meio dos testes do vizinho -> falhas NAO-DETERMINISTICAS e
#      espalhadas (StaleDataError, UNIQUE usuario.login, linhas que "somem").
#      So aparece com pytest concorrente; CI (1 processo) fica verde e escondia.
#   2. A suite dropava/limpava o padaria.db LOCAL do desenvolvedor.
# Fix: cada PROCESSO ganha seu proprio arquivo (worker do xdist quando ha, senao
# o PID). Respeita DATABASE_URL setado de proposito (ex: rodar contra Postgres).
# Tem que rodar ANTES de qualquer `from app import ...` (que importa config.py);
# por isso fica no topo do conftest.
_xdist_worker = os.environ.get('PYTEST_XDIST_WORKER')
# ARMADILHA do xdist (09/08/2026, 1678 erros "table usuario already exists"):
# o processo CONTROLADOR importa este conftest primeiro (sem
# PYTEST_XDIST_WORKER), seta DATABASE_URL=pid<controlador> no os.environ, e
# os WORKERS herdam essa env ao nascer — o conftest deles via a env
# preenchida, "respeitava" achando que foi o dev, e TODOS caiam no arquivo
# do controlador (create_all concorrente = tabela duplicada). O marcador
# _PADARIA_TEST_DB_AUTO distingue: env setada POR NOS pode ser sobrescrita
# pelo slot do worker; env setada pelo DEV (sem marcador) segue respeitada.
_db_auto = os.environ.get('_PADARIA_TEST_DB_AUTO')
if not os.environ.get('DATABASE_URL') or (_db_auto and _xdist_worker):
    import tempfile
    _db_slot = _xdist_worker or f'pid{os.getpid()}'
    os.environ['DATABASE_URL'] = (
        f'sqlite:///{tempfile.gettempdir()}/padaria_test_{_db_slot}.db')
    os.environ['_PADARIA_TEST_DB_AUTO'] = '1'

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
    # Pisos de estoque/throughput alteram deliberadamente os totais. Testes
    # antigos partem da demanda pura; os testes do piso o ligam explicitamente.
    application.config['SOURDOUGH_MIN_DIA'] = 0
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
    """Reseta o banco ao baseline entre testes (substitui o drop+create):

    1. Dropa indices que NAO sao do modelo — ou seja, adicionados por
       migrations (ex: `uq_estoque_loja_receita` de `_migrate_estoque_trava`).
       Como o schema agora eh compartilhado (nao recriado por teste), um teste
       que chama a migration deixava o indice unico vazar e quebrava testes
       seguintes que criam duplicatas de proposito. `create_all` nunca cria
       esses indices — sao so de migration.
    2. Apaga as linhas de todas as tabelas (ordem reversa de FK)."""
    from sqlalchemy import text
    try:
        model_idx = {idx.name for t in db.metadata.tables.values()
                     for idx in t.indexes}
        rows = db.session.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")).fetchall()
        for (name,) in rows:
            if name not in model_idx:
                db.session.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
    except Exception:  # noqa: BLE001
        db.session.rollback()
    for table in reversed(db.metadata.sorted_tables):
        db.session.execute(table.delete())
    db.session.commit()


@pytest.fixture(autouse=True)
def _localhost_como_host_de_loja(request, _app_session, monkeypatch):
    """Nos arquivos marcados `@pytest.mark.loja_host`, faz o host de teste
    (localhost) contar como host PÚBLICO da loja (LOJA_HOSTS=localhost). A loja
    só responde a anônimo nos hosts de LOJA_HOSTS — sem isso os testes de
    vitrine pública (LOJA_VISIVEL=1) cairiam em 404 no gate. Fica tudo em
    localhost (sem base_url/cookie). NÃO afeta os outros arquivos (gate de
    host segue valendo, ex.: testes de admin)."""
    if request.node.get_closest_marker('loja_host'):
        monkeypatch.setitem(_app_session.config, 'LOJA_HOSTS', 'localhost')


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
def owner_user(app):
    """Usuario com is_owner=True — necessario pra rotas @owner_required
    (ex: edicao de precos em massa)."""
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='dono teste', login='dono', papel='admin', is_owner=True)
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


@pytest.fixture
def congela_hoje(monkeypatch):
    """Congela `app.utils.hoje()`/`agora()` numa data FIXA (default: SEGUNDA
    17/08/2026, 10:00 BRT). Criada em 17/08/2026 junto da regra "produção só
    seg-sex": o shaping do cronograma passou a depender do dia da semana e os
    cenários hoje()-relativos (demanda em hoje+1..+4) quebrariam de quinta a
    domingo por FIXTURE caindo no fim de semana, não por bug. Uso: fixture
    autouse por arquivo chama `congela_hoje()`.

    Mecânica: `hoje()`/`agora()` chamam `datetime.now(BRT)` resolvendo o nome
    `datetime` no namespace de app.utils EM TEMPO DE CHAMADA — patchear
    `app.utils.datetime` congela os dois pra TODO importador (todos compartilham
    o mesmo objeto-função). Módulos que importaram `datetime` direto não são
    afetados (e não devem usar — regra do timezone no CLAUDE.md)."""
    def _congelar(ano=2026, mes=8, dia=17, hora=10):
        import app.utils as _u
        real = _u.datetime

        class _Congelado(real):
            @classmethod
            def now(cls, tz=None):
                base = real(ano, mes, dia, hora, 0, 0)
                return base.replace(tzinfo=tz) if tz is not None else base

        monkeypatch.setattr(_u, 'datetime', _Congelado)
    return _congelar

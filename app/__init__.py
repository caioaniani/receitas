import json

from flask import Flask

from app.extensions import db, csrf, login_manager
from config import Config


def create_app(config_class=None):
    app = Flask(__name__)
    app.config.from_object(config_class or Config)

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        from app.models import Usuario
        return Usuario.query.get(int(user_id))

    # ── Filtros Jinja2 ──
    @app.template_filter('brl')
    def brl_filter(value):
        if value is None:
            return 'R$ 0,00'
        formatted = f'{value:,.2f}'
        formatted = formatted.replace(',', 'X').replace('.', ',').replace('X', '.')
        return f'R$ {formatted}'

    # ── Context processor: sidebar com todas as receitas ──
    @app.context_processor
    def inject_sidebar():
        from flask_login import current_user
        from app.models import Receita, MateriaPrima, Usuario, Atribuicao

        # Sem queries para usuários não autenticados (ex: página de login)
        if not current_user.is_authenticated:
            return dict(
                sidebar_categorias={}, mp_json='{}', mp_nomes=[],
                receita_nomes=[], funcionarios=[],
            )

        # Uma única query de receitas (reusada para sidebar e nomes)
        receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()

        # Funcionário: filtrar só fichas atribuídas
        if not current_user.is_admin():
            ids_permitidos = {a.receita_id for a in
                             Atribuicao.query.filter_by(usuario_id=current_user.id).all()}
            receitas_sidebar = [r for r in receitas if r.id in ids_permitidos]
        else:
            receitas_sidebar = receitas

        categorias = {}
        for r in receitas_sidebar:
            cat = r.categoria or 'Outros'
            if cat not in categorias:
                categorias[cat] = []
            categorias[cat].append(r)

        # MP data como JSON para autocomplete
        mps = MateriaPrima.query.order_by(MateriaPrima.nome).all()
        mp_dict = {mp.nome: {'custo_por_kg': mp.custo_por_kg, 'unidade': mp.unidade} for mp in mps}
        mp_json = json.dumps(mp_dict, ensure_ascii=False)
        mp_nomes = [mp.nome for mp in mps]

        receita_nomes = [r.nome for r in receitas]

        # Lista de funcionários só para admin
        funcionarios = (
            Usuario.query.filter_by(papel='funcionario').order_by(Usuario.nome).all()
            if current_user.is_admin() else []
        )

        return dict(
            sidebar_categorias=categorias,
            mp_json=mp_json,
            mp_nomes=mp_nomes,
            receita_nomes=receita_nomes,
            funcionarios=funcionarios,
        )

    # ── Blueprints ──
    from app.blueprints.main import main_bp
    from app.blueprints.materias_primas import materias_primas_bp
    from app.blueprints.receitas import receitas_bp
    from app.blueprints.produtos import produtos_bp
    from app.blueprints.auth import auth_bp
    from app.blueprints.rh import rh_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(materias_primas_bp, url_prefix='/materias-primas')
    app.register_blueprint(receitas_bp, url_prefix='/receitas')
    app.register_blueprint(produtos_bp, url_prefix='/produtos')
    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(rh_bp, url_prefix='/rh')

    with app.app_context():
        db.create_all()
        _migrate(app)

        # Seed só roda localmente (SQLite) — em produção os dados já existem
        if app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite'):
            from app.seed import seed_database, seed_cardapio, seed_update_v2
            seed_database()
            seed_cardapio()
            seed_update_v2()

        # Produtos do site — roda em todos os ambientes (SQLite + PostgreSQL)
        from app.seed import seed_site_products
        seed_site_products()

        # RH: lojas + funcionários — roda em todos os ambientes
        from app.seed import seed_rh
        seed_rh()

        _criar_admin()

    return app


def _criar_admin():
    """Cria usuário admin padrão se não existir nenhum."""
    from app.models import Usuario
    if not Usuario.query.filter_by(papel='admin').first():
        admin = Usuario(nome='Admin', login='admin', papel='admin')
        admin.set_senha('admin')
        db.session.add(admin)
        db.session.commit()


def _migrate(app):
    """Adiciona colunas novas sem perder dados existentes."""
    uri = app.config['SQLALCHEMY_DATABASE_URI']

    if uri.startswith('sqlite'):
        _migrate_sqlite(app)
    elif 'postgresql' in uri:
        _migrate_postgres(app)


def _migrate_postgres(app):
    """Adiciona colunas novas no PostgreSQL."""
    from sqlalchemy import text
    with db.engine.connect() as conn:
        # Verificar e adicionar colunas faltantes em receita
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'receita'"
        ))
        colunas = {row[0] for row in result}

        migrações_receita = {
            'perda_percentual': 'ALTER TABLE receita ADD COLUMN perda_percentual REAL DEFAULT 0',
            'preco_loja': 'ALTER TABLE receita ADD COLUMN preco_loja REAL',
            'preco_site': 'ALTER TABLE receita ADD COLUMN preco_site REAL',
            'custo_embalagem': 'ALTER TABLE receita ADD COLUMN custo_embalagem REAL DEFAULT 0',
            'modo_preparo': 'ALTER TABLE receita ADD COLUMN modo_preparo TEXT',
            'observacao': 'ALTER TABLE receita ADD COLUMN observacao TEXT',
        }
        for col, sql in migrações_receita.items():
            if col not in colunas:
                conn.execute(text(sql))

        # receita_ingrediente
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'receita_ingrediente'"
        ))
        cols_ing = {row[0] for row in result}
        if cols_ing and 'tipo' not in cols_ing:
            conn.execute(text("ALTER TABLE receita_ingrediente ADD COLUMN tipo TEXT DEFAULT 'mp'"))

        # produto
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'produto'"
        ))
        cols_prod = {row[0] for row in result}
        if cols_prod:
            migrações_produto = {
                'custo_direto': 'ALTER TABLE produto ADD COLUMN custo_direto REAL',
                'custo_embalagem': 'ALTER TABLE produto ADD COLUMN custo_embalagem REAL DEFAULT 0',
                'modo_preparo': 'ALTER TABLE produto ADD COLUMN modo_preparo TEXT',
                'observacao': 'ALTER TABLE produto ADD COLUMN observacao TEXT',
            }
            for col, sql in migrações_produto.items():
                if col not in cols_prod:
                    conn.execute(text(sql))

        conn.commit()


def _migrate_sqlite(app):
    """Adiciona colunas novas no SQLite."""
    import sqlite3
    uri = app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '')
    conn = sqlite3.connect(uri)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(receita)")
    colunas = [row[1] for row in cursor.fetchall()]
    if 'perda_percentual' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN perda_percentual REAL DEFAULT 0")
    if 'preco_loja' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN preco_loja REAL")
    if 'preco_site' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN preco_site REAL")
    if 'custo_embalagem' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN custo_embalagem REAL DEFAULT 0")

    # Migração tabela receita_ingrediente
    cursor.execute("PRAGMA table_info(receita_ingrediente)")
    cols_ing = [row[1] for row in cursor.fetchall()]
    if cols_ing and 'tipo' not in cols_ing:
        cursor.execute("ALTER TABLE receita_ingrediente ADD COLUMN tipo TEXT DEFAULT 'mp'")

    # Migração tabela produto
    cursor.execute("PRAGMA table_info(produto)")
    cols_prod = [row[1] for row in cursor.fetchall()]
    if cols_prod and 'custo_direto' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN custo_direto REAL")
    if cols_prod and 'custo_embalagem' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN custo_embalagem REAL DEFAULT 0")
    if cols_prod and 'modo_preparo' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN modo_preparo TEXT")

    # Migração receita.modo_preparo
    if 'modo_preparo' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN modo_preparo TEXT")
    if 'observacao' not in colunas:
        cursor.execute("ALTER TABLE receita ADD COLUMN observacao TEXT")

    # Migração produto.observacao
    if cols_prod and 'observacao' not in cols_prod:
        cursor.execute("ALTER TABLE produto ADD COLUMN observacao TEXT")

    conn.commit()
    conn.close()

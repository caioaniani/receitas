import json

from flask import Flask

from app.extensions import db, csrf
from config import Config


def create_app(config_class=None):
    app = Flask(__name__)
    app.config.from_object(config_class or Config)

    db.init_app(app)
    csrf.init_app(app)

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
        from app.models import Receita, MateriaPrima
        receitas = Receita.query.order_by(Receita.categoria, Receita.nome).all()
        categorias = {}
        for r in receitas:
            cat = r.categoria or 'Outros'
            if cat not in categorias:
                categorias[cat] = []
            categorias[cat].append(r)

        # MP data como JSON para autocomplete
        mps = MateriaPrima.query.order_by(MateriaPrima.nome).all()
        mp_dict = {mp.nome: {'custo_por_kg': mp.custo_por_kg, 'unidade': mp.unidade} for mp in mps}
        mp_json = json.dumps(mp_dict, ensure_ascii=False)
        mp_nomes = [mp.nome for mp in mps]

        return dict(
            sidebar_categorias=categorias,
            mp_json=mp_json,
            mp_nomes=mp_nomes,
        )

    # ── Blueprints ──
    from app.blueprints.main import main_bp
    from app.blueprints.materias_primas import materias_primas_bp
    from app.blueprints.receitas import receitas_bp
    from app.blueprints.produtos import produtos_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(materias_primas_bp, url_prefix='/materias-primas')
    app.register_blueprint(receitas_bp, url_prefix='/receitas')
    app.register_blueprint(produtos_bp, url_prefix='/produtos')

    with app.app_context():
        db.create_all()
        _migrate(app)
        from app.seed import seed_database
        seed_database()

    return app


def _migrate(app):
    """Adiciona colunas novas sem perder dados existentes."""
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
    conn.commit()
    conn.close()

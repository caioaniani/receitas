import json
import os
from datetime import datetime

from flask import Flask, Response

from app.extensions import db, csrf, login_manager, limiter
from config import Config


def create_app(config_class=None):
    app = Flask(__name__)
    app.config.from_object(config_class or Config)

    if not os.environ.get('SECRET_KEY'):
        print('⚠️  SECRET_KEY não definida. Sessões expiram a cada restart. Defina no Railway.')

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    limiter.init_app(app)

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

    @app.context_processor
    def inject_now():
        return {'now': datetime.now}

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

        # Receitas com eager load de ingredientes (evita N+1 na sidebar)
        receitas = Receita.query.options(
            db.joinedload(Receita.ingredientes)
        ).order_by(Receita.categoria, Receita.nome).all()

        # Funcionário: filtrar só fichas atribuídas via subquery
        if not current_user.is_admin():
            ids_permitidos = set(
                db.session.query(Atribuicao.receita_id)
                .filter_by(usuario_id=current_user.id)
                .all()
            )
            ids_permitidos = {r[0] for r in ids_permitidos}
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
        mp_dict = {mp.nome: {'custo_por_kg': mp.custo_por_kg, 'unidade': mp.unidade,
                              'peso_unidade': mp.peso_unidade} for mp in mps}
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

    @app.route('/robots.txt')
    def robots_txt():
        return Response("User-agent: *\nDisallow: /\n", mimetype='text/plain')

    @app.after_request
    def add_security_headers(response):
        response.headers['X-Robots-Tag'] = 'noindex, nofollow'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            "font-src 'self' https://cdn.jsdelivr.net; "
            "img-src 'self' data:;"
        )
        return response

    # ── Error handlers ──
    @app.errorhandler(403)
    def forbidden(e):
        return render_template('errors/403.html'), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template('errors/404.html'), 404

    @app.errorhandler(500)
    def internal_error(e):
        return render_template('errors/500.html'), 500

    # ── Blueprints ──
    from app.blueprints.main import main_bp
    from app.blueprints.materias_primas import materias_primas_bp
    from app.blueprints.receitas import receitas_bp
    from app.blueprints.produtos import produtos_bp
    from app.blueprints.auth import auth_bp
    from app.blueprints.rh import rh_bp
    from app.blueprints.producao import producao_bp
    from app.blueprints.relatorios import relatorios_bp
    from app.blueprints.pedidos import pedidos_bp
    from app.blueprints.entregas import entregas_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(materias_primas_bp, url_prefix='/materias-primas')
    app.register_blueprint(receitas_bp, url_prefix='/receitas')
    app.register_blueprint(produtos_bp, url_prefix='/produtos')
    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(rh_bp, url_prefix='/rh')
    app.register_blueprint(producao_bp, url_prefix='/producao')
    app.register_blueprint(relatorios_bp, url_prefix='/relatorios')
    app.register_blueprint(pedidos_bp)
    app.register_blueprint(entregas_bp, url_prefix='/entregas')

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
        from app.seed import seed_rh, seed_rh_escala
        seed_rh()
        seed_rh_escala()

        _criar_admin()

    return app


def _criar_admin():
    """Cria usuário admin padrão se não existir nenhum."""
    from app.models import Usuario
    if not Usuario.query.filter_by(papel='admin').first():
        senha = os.environ.get('ADMIN_PASSWORD', 'admin')
        admin = Usuario(nome='Admin', login='admin', papel='admin')
        admin.set_senha(senha)
        db.session.add(admin)
        db.session.commit()
        if senha == 'admin':
            print('⚠️  Admin criado com senha padrão. Defina ADMIN_PASSWORD no Railway.')


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

        # funcionario
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'funcionario'"
        ))
        cols_func = {row[0] for row in result}
        if cols_func:
            migrações_func = {
                'funcao_operacional': 'ALTER TABLE funcionario ADD COLUMN funcao_operacional VARCHAR(100)',
                'periodo': 'ALTER TABLE funcionario ADD COLUMN periodo VARCHAR(20)',
                'cadastro_pendente': 'ALTER TABLE funcionario ADD COLUMN cadastro_pendente BOOLEAN DEFAULT FALSE',
                'data_nascimento': 'ALTER TABLE funcionario ADD COLUMN data_nascimento DATE',
            }
            for col, sql in migrações_func.items():
                if col not in cols_func:
                    conn.execute(text(sql))

        # loja
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'loja'"
        ))
        cols_loja = {row[0] for row in result}
        if cols_loja:
            if 'planta_imagem' not in cols_loja:
                conn.execute(text("ALTER TABLE loja ADD COLUMN planta_imagem BYTEA"))
            if 'planta_mimetype' not in cols_loja:
                conn.execute(text("ALTER TABLE loja ADD COLUMN planta_mimetype VARCHAR(100)"))

        # slot_mapa
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'slot_mapa'"
        ))
        cols_slot = {row[0] for row in result}
        if cols_slot:
            if 'largura' not in cols_slot:
                conn.execute(text("ALTER TABLE slot_mapa ADD COLUMN largura REAL DEFAULT 15"))
            if 'altura' not in cols_slot:
                conn.execute(text("ALTER TABLE slot_mapa ADD COLUMN altura REAL DEFAULT 8"))

        # usuario.loja_id
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'usuario'"
        ))
        cols_user = {row[0] for row in result}
        if cols_user and 'loja_id' not in cols_user:
            conn.execute(text("ALTER TABLE usuario ADD COLUMN loja_id INTEGER REFERENCES loja(id)"))

        # posicao.origem
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'posicao'"
        ))
        cols_pos = {row[0] for row in result}
        if cols_pos and 'origem' not in cols_pos:
            conn.execute(text("ALTER TABLE posicao ADD COLUMN origem VARCHAR(10) DEFAULT 'manual'"))
            conn.execute(text(
                "UPDATE posicao SET origem = 'mapa' WHERE EXISTS ("
                "  SELECT 1 FROM slot_mapa WHERE slot_mapa.loja_id = posicao.loja_id "
                "  AND slot_mapa.nome = posicao.nome_posicao)"
            ))

        # materia_prima.estoque_atual
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'materia_prima'"
        ))
        cols_mp = {row[0] for row in result}
        if cols_mp and 'estoque_atual' not in cols_mp:
            conn.execute(text("ALTER TABLE materia_prima ADD COLUMN estoque_atual REAL DEFAULT 0"))
        if cols_mp and 'peso_unidade' not in cols_mp:
            conn.execute(text("ALTER TABLE materia_prima ADD COLUMN peso_unidade REAL"))

        # pedido_item.quantidade_recebida + materia_prima_id
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'pedido_item'"
        ))
        cols_pi = {row[0] for row in result}
        if cols_pi and 'quantidade_recebida' not in cols_pi:
            conn.execute(text("ALTER TABLE pedido_item ADD COLUMN quantidade_recebida INTEGER"))
        if cols_pi and 'materia_prima_id' not in cols_pi:
            conn.execute(text("ALTER TABLE pedido_item ADD COLUMN materia_prima_id INTEGER REFERENCES materia_prima(id)"))

        # estoque_loja.materia_prima_id
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'estoque_loja'"
        ))
        cols_el = {row[0] for row in result}
        if cols_el and 'materia_prima_id' not in cols_el:
            conn.execute(text("ALTER TABLE estoque_loja ADD COLUMN materia_prima_id INTEGER REFERENCES materia_prima(id)"))

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

    # Migração funcionario
    cursor.execute("PRAGMA table_info(funcionario)")
    cols_func = [row[1] for row in cursor.fetchall()]
    if cols_func and 'funcao_operacional' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN funcao_operacional VARCHAR(100)")
    if cols_func and 'periodo' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN periodo VARCHAR(20)")
    if cols_func and 'cadastro_pendente' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN cadastro_pendente BOOLEAN DEFAULT 0")
    if cols_func and 'data_nascimento' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN data_nascimento DATE")

    # Migração loja
    cursor.execute("PRAGMA table_info(loja)")
    cols_loja = [row[1] for row in cursor.fetchall()]
    if cols_loja and 'planta_imagem' not in cols_loja:
        cursor.execute("ALTER TABLE loja ADD COLUMN planta_imagem BLOB")
    if cols_loja and 'planta_mimetype' not in cols_loja:
        cursor.execute("ALTER TABLE loja ADD COLUMN planta_mimetype VARCHAR(100)")

    # Migração slot_mapa
    cursor.execute("PRAGMA table_info(slot_mapa)")
    cols_slot = [row[1] for row in cursor.fetchall()]
    if cols_slot and 'largura' not in cols_slot:
        cursor.execute("ALTER TABLE slot_mapa ADD COLUMN largura REAL DEFAULT 15")
    if cols_slot and 'altura' not in cols_slot:
        cursor.execute("ALTER TABLE slot_mapa ADD COLUMN altura REAL DEFAULT 8")

    # Migração usuario.loja_id
    cursor.execute("PRAGMA table_info(usuario)")
    cols_user = [row[1] for row in cursor.fetchall()]
    if cols_user and 'loja_id' not in cols_user:
        cursor.execute("ALTER TABLE usuario ADD COLUMN loja_id INTEGER REFERENCES loja(id)")

    # Migração posicao.origem
    cursor.execute("PRAGMA table_info(posicao)")
    cols_pos = [row[1] for row in cursor.fetchall()]
    if cols_pos and 'origem' not in cols_pos:
        cursor.execute("ALTER TABLE posicao ADD COLUMN origem VARCHAR(10) DEFAULT 'manual'")
        cursor.execute(
            "UPDATE posicao SET origem = 'mapa' WHERE EXISTS ("
            "  SELECT 1 FROM slot_mapa WHERE slot_mapa.loja_id = posicao.loja_id "
            "  AND slot_mapa.nome = posicao.nome_posicao)"
        )

    # Migração materia_prima.estoque_atual + peso_unidade
    cursor.execute("PRAGMA table_info(materia_prima)")
    cols_mp = [row[1] for row in cursor.fetchall()]
    if cols_mp and 'estoque_atual' not in cols_mp:
        cursor.execute("ALTER TABLE materia_prima ADD COLUMN estoque_atual REAL DEFAULT 0")
    if cols_mp and 'peso_unidade' not in cols_mp:
        cursor.execute("ALTER TABLE materia_prima ADD COLUMN peso_unidade REAL")

    # Migração pedido_item.quantidade_recebida + materia_prima_id
    cursor.execute("PRAGMA table_info(pedido_item)")
    cols_pi = [row[1] for row in cursor.fetchall()]
    if cols_pi and 'quantidade_recebida' not in cols_pi:
        cursor.execute("ALTER TABLE pedido_item ADD COLUMN quantidade_recebida INTEGER")
    if cols_pi and 'materia_prima_id' not in cols_pi:
        cursor.execute("ALTER TABLE pedido_item ADD COLUMN materia_prima_id INTEGER REFERENCES materia_prima(id)")

    # Migração estoque_loja.materia_prima_id
    cursor.execute("PRAGMA table_info(estoque_loja)")
    cols_el = [row[1] for row in cursor.fetchall()]
    if cols_el and 'materia_prima_id' not in cols_el:
        cursor.execute("ALTER TABLE estoque_loja ADD COLUMN materia_prima_id INTEGER REFERENCES materia_prima(id)")

    conn.commit()
    conn.close()

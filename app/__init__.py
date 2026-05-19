import json
import os
from datetime import datetime

from flask import Flask, Response, render_template, request

from app.extensions import db, csrf, login_manager, limiter
from app.utils import agora as agora_brt
from config import Config


def _init_sentry():
    """Opt-in: so inicia se SENTRY_DSN estiver setado. Captura exceptions
    nao tratadas + breadcrumbs do Flask. PII desligado por default — nao
    queremos vazar nome de cliente em stack trace."""
    dsn = os.environ.get('SENTRY_DSN', '').strip()
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
        sentry_sdk.init(
            dsn=dsn,
            integrations=[FlaskIntegration(), SqlalchemyIntegration()],
            traces_sample_rate=float(os.environ.get('SENTRY_TRACES', '0.05')),
            send_default_pii=False,
            environment=os.environ.get('SENTRY_ENV', 'production'),
        )
    except ImportError:
        print('⚠️  sentry-sdk nao instalada — `pip install sentry-sdk[flask]`')


def create_app(config_class=None):
    _init_sentry()
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

    @app.template_filter('brt')
    def brt_filter(dt, fmt='%d/%m %H:%M'):
        """Formata datetime ja em BRT (sistema todo armazena BRT naive).

        Mantido como filtro pra padronizar o display de data/hora nos templates.
        """
        if not dt:
            return ''
        return dt.strftime(fmt)

    @app.context_processor
    def inject_now():
        return {'now': agora_brt}

    @app.context_processor
    def inject_static_version():
        """Versionamento de arquivos estaticos para cache busting.
        Usa hash MD5 do conteudo (8 chars) — muda sempre que arquivo muda,
        independente de mtime (Railway deploy nao preserva mtime original)."""
        import os
        import hashlib
        versions = {}
        for rel in ('js/projetos.js', 'js/app.js', 'js/entregas.js', 'js/copilot.js', 'css/style.css'):
            try:
                p = os.path.join(app.static_folder, rel)
                with open(p, 'rb') as f:
                    versions[rel] = hashlib.md5(f.read()).hexdigest()[:8]
            except OSError:
                versions[rel] = '0'
        return {'static_v': versions}

    # ── Context processor: sidebar com todas as receitas ──
    # Cache in-memory simples para a sidebar (queries pesadas que mudam pouco)
    import time as _time
    _SIDEBAR_CACHE = {}

    def _cache(key, ttl, factory):
        now = _time.time()
        item = _SIDEBAR_CACHE.get(key)
        if item and item['expires'] > now:
            return item['data']
        data = factory()
        _SIDEBAR_CACHE[key] = {'data': data, 'expires': now + ttl}
        return data

    def _invalidate_sidebar_cache():
        _SIDEBAR_CACHE.clear()

    # Expoe pra outros modulos invalidarem (ex: ao salvar receita/MP/projeto)
    app.invalidate_sidebar_cache = _invalidate_sidebar_cache

    # ── Auto-invalidação: limpa o cache quando algum modelo cacheado muda ──
    from sqlalchemy import event as _sa_event
    from app.models import Receita, MateriaPrima, Usuario, TarefaProjeto
    _MODELOS_CACHEADOS = (Receita, MateriaPrima, Usuario, TarefaProjeto)

    @_sa_event.listens_for(db.session, 'before_commit')
    def _invalidate_on_change(session):
        alvo = _MODELOS_CACHEADOS
        if any(isinstance(o, alvo) for o in session.new) \
                or any(isinstance(o, alvo) for o in session.dirty) \
                or any(isinstance(o, alvo) for o in session.deleted):
            _SIDEBAR_CACHE.clear()

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

        # ── Receitas + categorias (cache 60s) ──
        def _carrega_receitas_globais():
            recs = Receita.query.options(
                db.joinedload(Receita.ingredientes)
            ).order_by(Receita.categoria, Receita.nome).all()
            cats = {}
            for r in recs:
                cat = r.categoria or 'Outros'
                cats.setdefault(cat, []).append(r)
            return {
                'receitas': recs,
                'categorias': cats,
                'nomes': [r.nome for r in recs],
            }
        rec_data = _cache('receitas', 60, _carrega_receitas_globais)

        # Para não-admin, filtra por atribuições (NÃO cacheado, é per-user)
        if not current_user.is_admin():
            ids_permitidos = set(
                r[0] for r in db.session.query(Atribuicao.receita_id)
                .filter_by(usuario_id=current_user.id).all()
            )
            categorias = {}
            for cat, lst in rec_data['categorias'].items():
                filt = [r for r in lst if r.id in ids_permitidos]
                if filt:
                    categorias[cat] = filt
        else:
            categorias = rec_data['categorias']

        receita_nomes = rec_data['nomes']

        # ── MP data (cache 60s) ──
        def _carrega_mp_data():
            mps = MateriaPrima.query.order_by(MateriaPrima.nome).all()
            mp_dict = {mp.nome: {'custo_por_kg': mp.custo_por_kg, 'unidade': mp.unidade,
                                  'peso_unidade': mp.peso_unidade} for mp in mps}
            return {
                'json': json.dumps(mp_dict, ensure_ascii=False),
                'nomes': [mp.nome for mp in mps],
            }
        mp_data = _cache('mps', 60, _carrega_mp_data)

        # ── Funcionários (cache 120s, só admin precisa) ──
        if current_user.is_admin():
            def _carrega_funcs():
                return Usuario.query.filter_by(papel='funcionario').order_by(Usuario.nome).all()
            funcionarios = _cache('funcionarios', 120, _carrega_funcs)
        else:
            funcionarios = []

        # ── Contadores de Projetos (cache 10s, atualiza rapido) ──
        proj_atrasadas = 0
        proj_fazendo = 0
        if current_user.is_admin():
            try:
                def _carrega_proj_count():
                    from app.models import TarefaProjeto
                    from datetime import date as _date
                    a = TarefaProjeto.query.filter(
                        TarefaProjeto.prazo.isnot(None),
                        TarefaProjeto.prazo < _date.today(),
                        ~TarefaProjeto.status.in_(['feito', 'cancelado']),
                    ).count()
                    f = TarefaProjeto.query.filter_by(status='fazendo').count()
                    return (a, f)
                proj_atrasadas, proj_fazendo = _cache('proj_count', 10, _carrega_proj_count)
            except Exception:
                pass

        mp_json = mp_data['json']
        mp_nomes = mp_data['nomes']

        return dict(
            sidebar_categorias=categorias,
            mp_json=mp_json,
            mp_nomes=mp_nomes,
            receita_nomes=receita_nomes,
            funcionarios=funcionarios,
            proj_atrasadas=proj_atrasadas,
            proj_fazendo=proj_fazendo,
        )

    @app.route('/robots.txt')
    def robots_txt():
        return Response("User-agent: *\nDisallow: /\n", mimetype='text/plain')

    @app.route('/health')
    def health():
        """Endpoint leve para uptime checkers (pinga aqui pra evitar cold start)."""
        return 'ok', 200

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
        # Cache agressivo para assets estaticos (CSS/JS/fonts/imagens)
        if request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'public, max-age=86400, stale-while-revalidate=604800'
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
    from app.blueprints.driver import driver_bp
    from app.blueprints.comprovante import comprovante_bp
    from app.blueprints.projetos import projetos_bp
    from app.blueprints.b2b import b2b_bp
    from app.blueprints.handshake import handshake_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(materias_primas_bp, url_prefix='/materias-primas')
    app.register_blueprint(receitas_bp, url_prefix='/receitas')
    app.register_blueprint(produtos_bp, url_prefix='/produtos')
    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(rh_bp, url_prefix='/rh')
    app.register_blueprint(producao_bp, url_prefix='/producao')
    app.register_blueprint(relatorios_bp, url_prefix='/relatorios')
    app.register_blueprint(b2b_bp, url_prefix='/b2b')
    app.register_blueprint(handshake_bp, url_prefix='/handshake')
    app.register_blueprint(pedidos_bp)
    app.register_blueprint(entregas_bp, url_prefix='/entregas')
    app.register_blueprint(driver_bp, url_prefix='/driver')
    app.register_blueprint(comprovante_bp, url_prefix='/entrega')
    app.register_blueprint(projetos_bp)
    from app.blueprints.pdv import pdv_bp
    app.register_blueprint(pdv_bp, url_prefix='/pdv')
    from app.blueprints.bot import bot_bp
    app.register_blueprint(bot_bp)
    from app.blueprints.copilot import copilot_bp
    app.register_blueprint(copilot_bp)
    from app.blueprints.fornecedores import fornecedores_bp
    app.register_blueprint(fornecedores_bp)
    from app.blueprints.slack import slack_bp
    app.register_blueprint(slack_bp)

    # Ativa audit log (listeners SQLAlchemy)
    from app.services.audit import init_audit
    init_audit()

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

        # Gestão de Projetos — seed inicial em todos os ambientes
        from app.seed import seed_projetos
        seed_projetos()

        _criar_admin()

    # Cron de auto-sync Seru → EstoqueLoja (15min). Roda dentro de
    # cada worker gunicorn mas usa pg_try_advisory_lock pra deduplicate.
    if not app.config.get('TESTING'):
        try:
            from app.services import seru_cron
            seru_cron.iniciar(app)
        except Exception as e:
            app.logger.warning('Nao foi possivel iniciar seru auto-sync: %s', e)

    return app


def _criar_admin():
    """Cria usuário admin padrão se não existir nenhum."""
    from app.models import Usuario
    if not Usuario.query.filter_by(papel='admin').first():
        senha = os.environ.get('ADMIN_PASSWORD', 'admin')
        admin = Usuario(nome='Admin', login='admin', papel='admin', is_owner=True)
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
    """Adiciona colunas novas no PostgreSQL. Cada ALTER em commit isolado
    para que falhas pontuais não abortem migrations seguintes."""
    from sqlalchemy import text
    import logging
    log = logging.getLogger(__name__)

    def _try(stmt):
        """Executa um DDL em sub-conexão isolada com commit imediato."""
        try:
            with db.engine.connect() as c:
                c.execute(text(stmt))
                c.commit()
        except Exception as e:
            log.warning('migrate skip (%s): %s', stmt[:60], e)

    def _cols(table):
        try:
            with db.engine.connect() as c:
                r = c.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :t"), {'t': table})
                return {row[0] for row in r}
        except Exception:
            return set()

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
                'horas_extras': 'ALTER TABLE funcionario ADD COLUMN horas_extras REAL DEFAULT 0',
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

        # estoque_loja.materia_prima_id + nome_pendente
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'estoque_loja'"
        ))
        cols_el = {row[0] for row in result}
        if cols_el and 'materia_prima_id' not in cols_el:
            conn.execute(text("ALTER TABLE estoque_loja ADD COLUMN materia_prima_id INTEGER REFERENCES materia_prima(id)"))
        if cols_el and 'nome_pendente' not in cols_el:
            conn.execute(text("ALTER TABLE estoque_loja ADD COLUMN nome_pendente VARCHAR(200)"))

        # Tabelas Seru (mapeamento + idempotencia). db.create_all() cria
        # automaticamente, este bloco e so safety pra ambientes que ja
        # existiam antes do schema.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS seru_produto_map (
                id SERIAL PRIMARY KEY,
                seru_nome VARCHAR(300) NOT NULL UNIQUE,
                seru_sku VARCHAR(100),
                receita_id INTEGER REFERENCES receita(id),
                produto_id INTEGER REFERENCES produto(id),
                ignorar BOOLEAN NOT NULL DEFAULT FALSE,
                primeira_visto_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmado_em TIMESTAMP,
                confirmado_por INTEGER REFERENCES usuario(id)
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_seru_produto_nome ON seru_produto_map(seru_nome)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS seru_loja_map (
                id SERIAL PRIMARY KEY,
                seru_company_name VARCHAR(300) NOT NULL UNIQUE,
                loja_id INTEGER REFERENCES loja(id),
                ignorar BOOLEAN NOT NULL DEFAULT FALSE,
                auto_match BOOLEAN DEFAULT FALSE,
                confirmado_em TIMESTAMP,
                confirmado_por INTEGER REFERENCES usuario(id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS seru_pedido_processado (
                seru_pedido_id VARCHAR(100) PRIMARY KEY,
                processado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                loja_id INTEGER REFERENCES loja(id),
                n_itens_total INTEGER DEFAULT 0,
                n_itens_baixados INTEGER DEFAULT 0,
                cancelado_em TIMESTAMP,
                estornado_em TIMESTAMP
            )
        """))

        # Coluna nova em SeruProdutoMap pra produtos compostos/fracionados
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'seru_produto_map'"
        ))
        cols_spm = {row[0] for row in result}
        if cols_spm and 'fator_quantidade' not in cols_spm:
            conn.execute(text("ALTER TABLE seru_produto_map ADD COLUMN fator_quantidade REAL NOT NULL DEFAULT 1.0"))

        # Acumulador de baixas fracionadas
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS seru_debito (
                loja_id INTEGER NOT NULL REFERENCES loja(id),
                seru_produto_map_id INTEGER NOT NULL REFERENCES seru_produto_map(id) ON DELETE CASCADE,
                fracao_pendente REAL NOT NULL DEFAULT 0.0,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (loja_id, seru_produto_map_id)
            )
        """))

        # mov_estoque_loja.tipo: VARCHAR(20) era curto pra 'venda_seru_sem_estoque' (22)
        try:
            conn.execute(text("ALTER TABLE mov_estoque_loja ALTER COLUMN tipo TYPE VARCHAR(50)"))
        except Exception:
            pass

        # Tabelas VNDA (mapeamento + idempotencia + acumulador fracionario)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS vnda_produto_map (
                id SERIAL PRIMARY KEY,
                vnda_nome VARCHAR(300) NOT NULL UNIQUE,
                vnda_sku VARCHAR(100),
                receita_id INTEGER REFERENCES receita(id),
                produto_id INTEGER REFERENCES produto(id),
                ignorar BOOLEAN NOT NULL DEFAULT FALSE,
                fator_quantidade REAL NOT NULL DEFAULT 1.0,
                primeira_visto_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmado_em TIMESTAMP,
                confirmado_por INTEGER REFERENCES usuario(id)
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_vnda_produto_nome ON vnda_produto_map(vnda_nome)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS vnda_pedido_processado (
                vnda_pedido_code VARCHAR(100) PRIMARY KEY,
                processado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                data_entrega DATE,
                n_itens_total INTEGER DEFAULT 0,
                n_itens_baixados INTEGER DEFAULT 0,
                cancelado_em TIMESTAMP,
                estornado_em TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS vnda_debito (
                vnda_produto_map_id INTEGER NOT NULL REFERENCES vnda_produto_map(id) ON DELETE CASCADE,
                componente_key VARCHAR(50) NOT NULL DEFAULT 'self',
                fracao_pendente REAL NOT NULL DEFAULT 0.0,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (vnda_produto_map_id, componente_key)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS app_config (
                key VARCHAR(100) PRIMARY KEY,
                value TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS loja_produto_map (
                id SERIAL PRIMARY KEY,
                nome_digitado VARCHAR(200) NOT NULL UNIQUE,
                receita_id INTEGER REFERENCES receita(id),
                produto_id INTEGER REFERENCES produto(id),
                materia_prima_id INTEGER REFERENCES materia_prima(id),
                ignorar BOOLEAN DEFAULT FALSE NOT NULL,
                fator_quantidade REAL NOT NULL DEFAULT 1.0,
                primeira_visto_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmado_em TIMESTAMP,
                confirmado_por INTEGER REFERENCES usuario(id)
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_loja_produto_map_nome ON loja_produto_map(nome_digitado)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS loja_debito (
                loja_id INTEGER NOT NULL REFERENCES loja(id),
                loja_produto_map_id INTEGER NOT NULL REFERENCES loja_produto_map(id) ON DELETE CASCADE,
                fracao_pendente REAL NOT NULL DEFAULT 0.0,
                atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (loja_id, loja_produto_map_id)
            )
        """))

        # estoque_producao.nome_pendente (balanco aceita itens sem cadastro previo)
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'estoque_producao'"
        ))
        cols_ep = {row[0] for row in result}
        if cols_ep and 'nome_pendente' not in cols_ep:
            conn.execute(text("ALTER TABLE estoque_producao ADD COLUMN nome_pendente VARCHAR(200)"))

        # vnda_debito.componente_key (cestas: PK composta para 1 acumulador por componente)
        result = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'vnda_debito'"
        ))
        cols_vd = {row[0] for row in result}
        if cols_vd and 'componente_key' not in cols_vd:
            conn.execute(text("ALTER TABLE vnda_debito DROP CONSTRAINT IF EXISTS vnda_debito_pkey"))
            conn.execute(text(
                "ALTER TABLE vnda_debito ADD COLUMN componente_key VARCHAR(50) NOT NULL DEFAULT 'self'"
            ))
            conn.execute(text(
                "ALTER TABLE vnda_debito ADD PRIMARY KEY (vnda_produto_map_id, componente_key)"
            ))

        conn.commit()

    # Migrações resilientes (cada ALTER em sua própria transação)
    cols_user2 = _cols('usuario')
    if cols_user2 and 'is_owner' not in cols_user2:
        _try("ALTER TABLE usuario ADD COLUMN is_owner BOOLEAN DEFAULT FALSE")
        _try("UPDATE usuario SET is_owner = TRUE WHERE id = "
             "(SELECT id FROM usuario WHERE papel = 'admin' ORDER BY id LIMIT 1)")

    # Migracao papel_v1: introduz niveis (gerente/producao/rh). Roda uma vez.
    # Downgrade de admins NAO-owner pra funcionario; owner sobe pra papel='admin' se ainda nao for.
    _try("CREATE TABLE IF NOT EXISTS migracao_marker (nome VARCHAR(50) PRIMARY KEY, executado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    try:
        with db.engine.connect() as c:
            r = c.execute(text("SELECT 1 FROM migracao_marker WHERE nome='papel_v1'")).fetchone()
            ja_rodou = bool(r)
    except Exception:
        ja_rodou = True  # se nao consegue ler, nao mexe
    if not ja_rodou:
        _try("UPDATE usuario SET papel='funcionario' WHERE papel='admin' AND (is_owner IS NULL OR is_owner = FALSE)")
        _try("UPDATE usuario SET papel='admin' WHERE is_owner = TRUE AND papel <> 'admin'")
        _try("INSERT INTO migracao_marker (nome) VALUES ('papel_v1')")

    cols_pa = _cols('projeto_area')
    if cols_pa and 'cor' not in cols_pa:
        _try("ALTER TABLE projeto_area ADD COLUMN cor VARCHAR(20)")

    cols_tp = _cols('tarefa_projeto')
    if cols_tp and 'observacao' not in cols_tp:
        _try("ALTER TABLE tarefa_projeto ADD COLUMN observacao TEXT")
    if cols_tp and 'recorrencia' not in cols_tp:
        _try("ALTER TABLE tarefa_projeto ADD COLUMN recorrencia VARCHAR(20)")

    cols_func_res = _cols('funcionario')
    if cols_func_res and 'horas_extras' not in cols_func_res:
        _try("ALTER TABLE funcionario ADD COLUMN horas_extras REAL DEFAULT 0")
    if cols_func_res and 'tem_cargo_confianca' not in cols_func_res:
        _try("ALTER TABLE funcionario ADD COLUMN tem_cargo_confianca BOOLEAN DEFAULT FALSE")
        # Liga a flag para quem ja tinha cargo_confianca > 0 (preserva comportamento)
        _try("UPDATE funcionario SET tem_cargo_confianca = TRUE WHERE cargo_confianca > 0")

    # Cargo: cria a coluna FK + popula cargos a partir das funcoes existentes
    if cols_func_res and 'cargo_id' not in cols_func_res:
        _try("ALTER TABLE funcionario ADD COLUMN cargo_id INTEGER REFERENCES cargo(id)")
        # Cria 1 cargo por funcao distinta com o salario MAIS COMUM (moda) de quem tem essa funcao
        _try("""
        INSERT INTO cargo (nome, salario_base, ativo)
        SELECT funcao, MAX(salario_base), TRUE
        FROM funcionario
        WHERE funcao IS NOT NULL AND TRIM(funcao) <> ''
        GROUP BY funcao
        ON CONFLICT (nome) DO NOTHING
        """)
        # Liga cada funcionario ao cargo correspondente
        _try("""
        UPDATE funcionario SET cargo_id = (
            SELECT id FROM cargo WHERE cargo.nome = funcionario.funcao
        ) WHERE funcao IS NOT NULL AND TRIM(funcao) <> ''
        """)

    # ── Override de data de entrega de pedido VNDA (local) ──
    _try("""
    CREATE TABLE IF NOT EXISTS override_entrega (
        id SERIAL PRIMARY KEY,
        pedido_code VARCHAR(50) NOT NULL UNIQUE,
        data_entrega DATE NOT NULL,
        motivo TEXT,
        atualizado_em TIMESTAMP DEFAULT NOW(),
        atualizado_por INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_override_entrega_code ON override_entrega(pedido_code)")

    # ── Cache de geocoding (CEP/endereco -> lat/lng) ──
    _try("""
    CREATE TABLE IF NOT EXISTS geocode_cache (
        id SERIAL PRIMARY KEY,
        chave VARCHAR(200) NOT NULL UNIQUE,
        lat DOUBLE PRECISION,
        lng DOUBLE PRECISION,
        fonte VARCHAR(50),
        criado_em TIMESTAMP DEFAULT NOW()
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_geocode_cache_chave ON geocode_cache(chave)")
    # Aumenta coluna fonte caso ja exista com VARCHAR(20)
    _try("ALTER TABLE geocode_cache ALTER COLUMN fonte TYPE VARCHAR(50)")
    # Limpa cache de falhas legacy (Nominatim/BrasilAPI/AwesomeAPI/google_fail).
    # Endereco volta a ser geocodado na proxima execucao via Google.
    _try("DELETE FROM geocode_cache WHERE lat IS NULL")

    # ── Drivers de entrega + atribuicoes pedido<->driver ──
    _try("""
    CREATE TABLE IF NOT EXISTS driver_entrega (
        id SERIAL PRIMARY KEY,
        nome VARCHAR(80) NOT NULL UNIQUE,
        cor VARCHAR(20),
        telefone VARCHAR(30),
        ativo BOOLEAN DEFAULT TRUE,
        criado_em TIMESTAMP DEFAULT NOW()
    )
    """)
    _try("""
    CREATE TABLE IF NOT EXISTS atribuicao_entrega (
        id SERIAL PRIMARY KEY,
        pedido_code VARCHAR(50) NOT NULL UNIQUE,
        driver_id INTEGER REFERENCES driver_entrega(id) ON DELETE SET NULL,
        data_entrega DATE,
        ordem INTEGER DEFAULT 0,
        atualizado_em TIMESTAMP DEFAULT NOW(),
        atualizado_por INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_atribuicao_pedido ON atribuicao_entrega(pedido_code)")
    _try("CREATE INDEX IF NOT EXISTS idx_atribuicao_data ON atribuicao_entrega(data_entrega)")

    # ── Comprovante de entrega: token+pin no driver, status+geo+fotos na atribuicao ──
    _try("ALTER TABLE driver_entrega ADD COLUMN token VARCHAR(32)")
    _try("CREATE UNIQUE INDEX IF NOT EXISTS idx_driver_token ON driver_entrega(token)")
    _try("ALTER TABLE driver_entrega ADD COLUMN pin VARCHAR(8)")
    _try("ALTER TABLE driver_entrega ADD COLUMN capacidade INTEGER DEFAULT 999")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN status VARCHAR(20) DEFAULT 'pendente'")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN entregue_em TIMESTAMP")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN nota VARCHAR(500)")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN motivo_falha VARCHAR(50)")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN geo_lat DOUBLE PRECISION")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN geo_lng DOUBLE PRECISION")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN proof_hash VARCHAR(32)")
    _try("CREATE UNIQUE INDEX IF NOT EXISTS idx_atribuicao_proof_hash ON atribuicao_entrega(proof_hash)")
    _try("""
    CREATE TABLE IF NOT EXISTS entrega_foto (
        id SERIAL PRIMARY KEY,
        atribuicao_id INTEGER NOT NULL REFERENCES atribuicao_entrega(id) ON DELETE CASCADE,
        url VARCHAR(500) NOT NULL,
        storage_path VARCHAR(500),
        tirada_em TIMESTAMP DEFAULT NOW(),
        tamanho_bytes INTEGER
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_entrega_foto_atribuicao ON entrega_foto(atribuicao_id)")

    # ── Lotes de saída ──
    _try("""
    CREATE TABLE IF NOT EXISTS lote_saida (
        id SERIAL PRIMARY KEY,
        nome VARCHAR(120) NOT NULL,
        data_entrega DATE NOT NULL,
        criado_em TIMESTAMP DEFAULT NOW(),
        janelas_json TEXT,
        status VARCHAR(20) DEFAULT 'aberto',
        criado_por INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_lote_saida_data ON lote_saida(data_entrega)")
    _try("CREATE INDEX IF NOT EXISTS idx_lote_saida_status ON lote_saida(status)")
    _try("ALTER TABLE atribuicao_entrega ADD COLUMN lote_id INTEGER REFERENCES lote_saida(id)")
    _try("CREATE INDEX IF NOT EXISTS idx_atribuicao_lote ON atribuicao_entrega(lote_id)")

    # ── Audit Log estruturado ──
    _try("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id SERIAL PRIMARY KEY,
        usuario_id INTEGER REFERENCES usuario(id),
        criado_em TIMESTAMP DEFAULT NOW(),
        tabela VARCHAR(60) NOT NULL,
        registro_id INTEGER,
        acao VARCHAR(10) NOT NULL,
        antes TEXT,
        depois TEXT,
        ip VARCHAR(45),
        user_agent VARCHAR(300)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_audit_usuario ON audit_log(usuario_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_audit_criado ON audit_log(criado_em)")
    _try("CREATE INDEX IF NOT EXISTS idx_audit_tabela ON audit_log(tabela)")
    _try("CREATE INDEX IF NOT EXISTS idx_audit_registro ON audit_log(tabela, registro_id)")

    # ── Fornecedores + historico de preco MP ──
    _try("""
    CREATE TABLE IF NOT EXISTS fornecedor (
        id SERIAL PRIMARY KEY,
        nome VARCHAR(150) NOT NULL UNIQUE,
        cnpj VARCHAR(20),
        telefone VARCHAR(30),
        email VARCHAR(120),
        contato VARCHAR(100),
        observacao TEXT,
        ativo BOOLEAN DEFAULT TRUE,
        criado_em TIMESTAMP DEFAULT NOW()
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_fornecedor_ativo ON fornecedor(ativo)")
    _try("""
    CREATE TABLE IF NOT EXISTS historico_preco_mp (
        id SERIAL PRIMARY KEY,
        materia_prima_id INTEGER NOT NULL REFERENCES materia_prima(id),
        fornecedor_id INTEGER NOT NULL REFERENCES fornecedor(id),
        preco_unitario DOUBLE PRECISION NOT NULL,
        quantidade DOUBLE PRECISION NOT NULL,
        data TIMESTAMP DEFAULT NOW(),
        referencia VARCHAR(200),
        usuario_id INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_hpm_mp ON historico_preco_mp(materia_prima_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_hpm_fornecedor ON historico_preco_mp(fornecedor_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_hpm_data ON historico_preco_mp(data)")
    _try("ALTER TABLE movimentacao_estoque ADD COLUMN fornecedor_id INTEGER REFERENCES fornecedor(id)")

    # ── Copilot conversas (audit trail das interacoes com LLM) ──
    _try("""
    CREATE TABLE IF NOT EXISTS copilot_conversa (
        id SERIAL PRIMARY KEY,
        usuario_id INTEGER NOT NULL REFERENCES usuario(id),
        criado_em TIMESTAMP DEFAULT NOW(),
        prompt TEXT NOT NULL,
        interpretacao_json TEXT,
        tipo_acao VARCHAR(40),
        status VARCHAR(20) DEFAULT 'pendente',
        executado_em TIMESTAMP,
        registro_tipo VARCHAR(40),
        registro_id INTEGER,
        erro TEXT
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_copilot_usuario ON copilot_conversa(usuario_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_copilot_criado ON copilot_conversa(criado_em)")
    _try("CREATE INDEX IF NOT EXISTS idx_copilot_status ON copilot_conversa(status)")

    # Backfill: cada data com atribuicoes orfas vira 1 lote 'Histórico DD/MM' concluido.
    # Idempotente — so cria se ainda houver lote_id NULL.
    _try("""
    INSERT INTO lote_saida (nome, data_entrega, criado_em, status)
    SELECT
        'Histórico ' || TO_CHAR(data_entrega, 'DD/MM/YYYY'),
        data_entrega,
        COALESCE(MIN(atualizado_em), NOW()),
        'concluido'
    FROM atribuicao_entrega
    WHERE lote_id IS NULL AND data_entrega IS NOT NULL
    GROUP BY data_entrega
    """)
    _try("""
    UPDATE atribuicao_entrega a
    SET lote_id = l.id
    FROM lote_saida l
    WHERE a.lote_id IS NULL
      AND a.data_entrega = l.data_entrega
      AND l.nome LIKE 'Histórico %'
    """)

    # ── Pedidos cadastrados fora do VNDA (manuais) ──
    _try("""
    CREATE TABLE IF NOT EXISTS pedido_local (
        id SERIAL PRIMARY KEY,
        code VARCHAR(50) NOT NULL UNIQUE,
        destinatario VARCHAR(200) NOT NULL,
        telefone VARCHAR(50) NOT NULL,
        endereco VARCHAR(500) NOT NULL,
        data_entrega DATE NOT NULL,
        periodo VARCHAR(80),
        cartinha TEXT,
        observacao TEXT,
        criado_em TIMESTAMP DEFAULT NOW(),
        criado_por INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_pedido_local_data ON pedido_local(data_entrega)")
    _try("""
    CREATE TABLE IF NOT EXISTS pedido_local_item (
        id SERIAL PRIMARY KEY,
        pedido_local_id INTEGER NOT NULL REFERENCES pedido_local(id) ON DELETE CASCADE,
        nome VARCHAR(200) NOT NULL,
        quantidade INTEGER DEFAULT 1,
        preco_unitario REAL DEFAULT 0
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_pedido_local_item_pedido ON pedido_local_item(pedido_local_id)")

    # ── Desperdicio (sobra do dia / vencido) ──
    _try("""
    CREATE TABLE IF NOT EXISTS desperdicio (
        id SERIAL PRIMARY KEY,
        loja_id INTEGER NOT NULL REFERENCES loja(id),
        receita_id INTEGER REFERENCES receita(id),
        produto_id INTEGER REFERENCES produto(id),
        materia_prima_id INTEGER REFERENCES materia_prima(id),
        quantidade INTEGER NOT NULL,
        data DATE NOT NULL,
        motivo VARCHAR(30) NOT NULL DEFAULT 'vencido',
        observacao TEXT,
        criado_em TIMESTAMP DEFAULT NOW(),
        criado_por_id INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_desperdicio_loja ON desperdicio(loja_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_desperdicio_data ON desperdicio(data)")

    # ── Slack bot (DM/@mention → copilot) ──
    _try("""
    CREATE TABLE IF NOT EXISTS slack_vinculo (
        id SERIAL PRIMARY KEY,
        slack_user_id VARCHAR(30) NOT NULL UNIQUE,
        slack_workspace_id VARCHAR(30),
        usuario_id INTEGER NOT NULL REFERENCES usuario(id),
        ativo BOOLEAN DEFAULT TRUE,
        criado_em TIMESTAMP DEFAULT NOW(),
        criado_por_id INTEGER REFERENCES usuario(id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_slack_vinculo_uid ON slack_vinculo(slack_user_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_slack_vinculo_ativo ON slack_vinculo(ativo)")

    _try("""
    CREATE TABLE IF NOT EXISTS slack_evento_processado (
        event_id VARCHAR(50) PRIMARY KEY,
        processado_em TIMESTAMP DEFAULT NOW()
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_slack_evento_em ON slack_evento_processado(processado_em)")

    _try("""
    CREATE TABLE IF NOT EXISTS slack_acao_pendente (
        id SERIAL PRIMARY KEY,
        token VARCHAR(40) NOT NULL UNIQUE,
        slack_user_id VARCHAR(30) NOT NULL,
        slack_channel_id VARCHAR(30),
        slack_message_ts VARCHAR(30),
        tipo_acao VARCHAR(50) NOT NULL,
        params_json TEXT NOT NULL,
        usuario_id INTEGER NOT NULL REFERENCES usuario(id),
        criado_em TIMESTAMP DEFAULT NOW(),
        executado_em TIMESTAMP,
        cancelado_em TIMESTAMP
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_slack_acao_token ON slack_acao_pendente(token)")
    _try("CREATE INDEX IF NOT EXISTS idx_slack_acao_em ON slack_acao_pendente(criado_em)")

    _try("""
    CREATE TABLE IF NOT EXISTS slack_conversa (
        id SERIAL PRIMARY KEY,
        slack_user_id VARCHAR(30) NOT NULL,
        slack_channel_id VARCHAR(30) NOT NULL,
        mensagens_json TEXT DEFAULT '[]',
        ultima_msg_em TIMESTAMP DEFAULT NOW(),
        UNIQUE (slack_user_id, slack_channel_id)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_slack_conversa_uid ON slack_conversa(slack_user_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_slack_conversa_em ON slack_conversa(ultima_msg_em)")

    # ── Lembrete de pedido pra amanha (opt-out por loja+data) ──
    _try("""
    CREATE TABLE IF NOT EXISTS lembrete_pedido_optout (
        id SERIAL PRIMARY KEY,
        loja_id INTEGER NOT NULL REFERENCES loja(id),
        data_entrega DATE NOT NULL,
        marcado_por_slack_uid VARCHAR(30),
        marcado_por_id INTEGER REFERENCES usuario(id),
        criado_em TIMESTAMP DEFAULT NOW(),
        UNIQUE (loja_id, data_entrega)
    )
    """)
    _try("CREATE INDEX IF NOT EXISTS idx_lembrete_optout_data ON lembrete_pedido_optout(data_entrega)")

    # Indices em tabelas que crescem por movimentacao — historico de estoque
    # e itens de pedido sao consultados muito por FK (estoque_loja_id,
    # estoque_producao_id, pedido_id) e ordenados por data. Sem indice,
    # cada listagem vira full-scan.
    _try("CREATE INDEX IF NOT EXISTS idx_mov_estoque_loja_el ON mov_estoque_loja(estoque_loja_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_mov_estoque_loja_data ON mov_estoque_loja(data)")
    _try("CREATE INDEX IF NOT EXISTS idx_mov_estoque_producao_ep ON mov_estoque_producao(estoque_producao_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_mov_estoque_producao_data ON mov_estoque_producao(data)")
    _try("CREATE INDEX IF NOT EXISTS idx_pedido_item_pedido ON pedido_item(pedido_id)")

    # B2B — venda da industria pra clientes externos. db.create_all cria as
    # tabelas no boot; aqui so adicionamos indices uteis e migracoes futuras.
    # Tabela preco_atacado foi criada por engano (preco ja existe em
    # Receita.preco_venda e Produto.preco_atacado). Dropa se existir.
    _try("DROP TABLE IF EXISTS preco_atacado")
    _try("CREATE INDEX IF NOT EXISTS idx_venda_b2b_data ON venda_b2b(data_venda)")
    _try("CREATE INDEX IF NOT EXISTS idx_venda_b2b_cliente ON venda_b2b(cliente_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_venda_b2b_item_venda ON venda_b2b_item(venda_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_venda_b2b_parcela_venda ON venda_b2b_parcela(venda_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_venda_b2b_parcela_venc ON venda_b2b_parcela(vencimento)")

    # Handshake QR Code — PIN da loja + tokens curtos por pedido.
    _try("ALTER TABLE loja ADD COLUMN IF NOT EXISTS pin VARCHAR(8)")
    _try("CREATE INDEX IF NOT EXISTS idx_pedido_qrcode_token ON pedido_qrcode(token)")
    _try("CREATE INDEX IF NOT EXISTS idx_pedido_qrcode_pedido ON pedido_qrcode(pedido_id)")

    # Vendas manuais pra lojas sem API (Anesio): so alimenta previsao /
    # sugestao de pedido. db.create_all cria a tabela; aqui so indices.
    _try("CREATE INDEX IF NOT EXISTS idx_venda_manual_loja ON venda_manual_loja(loja_id)")
    _try("CREATE INDEX IF NOT EXISTS idx_venda_manual_data ON venda_manual_loja(data_venda)")

    # Backfill de tokens em drivers existentes (sem token)
    try:
        import secrets
        from app.models import Driver
        sem_token = Driver.query.filter(
            (Driver.token == None) | (Driver.token == '')  # noqa: E711
        ).all()
        for drv in sem_token:
            drv.token = secrets.token_urlsafe(16)
        if sem_token:
            db.session.commit()
    except Exception as e:
        app.logger.warning('backfill token driver falhou: %s', e)
        db.session.rollback()


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
    if cols_func and 'horas_extras' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN horas_extras REAL DEFAULT 0")
    if cols_func and 'tem_cargo_confianca' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN tem_cargo_confianca BOOLEAN DEFAULT 0")
        cursor.execute("UPDATE funcionario SET tem_cargo_confianca = 1 WHERE cargo_confianca > 0")
    if cols_func and 'cargo_id' not in cols_func:
        cursor.execute("ALTER TABLE funcionario ADD COLUMN cargo_id INTEGER REFERENCES cargo(id)")
        cursor.execute("""
            INSERT OR IGNORE INTO cargo (nome, salario_base, ativo)
            SELECT funcao, MAX(salario_base), 1
            FROM funcionario
            WHERE funcao IS NOT NULL AND TRIM(funcao) <> ''
            GROUP BY funcao
        """)
        cursor.execute("""
            UPDATE funcionario SET cargo_id = (
                SELECT id FROM cargo WHERE cargo.nome = funcionario.funcao
            ) WHERE funcao IS NOT NULL AND TRIM(funcao) <> ''
        """)

    # Migração loja
    cursor.execute("PRAGMA table_info(loja)")
    cols_loja = [row[1] for row in cursor.fetchall()]
    if cols_loja and 'planta_imagem' not in cols_loja:
        cursor.execute("ALTER TABLE loja ADD COLUMN planta_imagem BLOB")
    if cols_loja and 'planta_mimetype' not in cols_loja:
        cursor.execute("ALTER TABLE loja ADD COLUMN planta_mimetype VARCHAR(100)")
    if cols_loja and 'pin' not in cols_loja:
        cursor.execute("ALTER TABLE loja ADD COLUMN pin VARCHAR(8)")

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

    # Migração usuario.is_owner
    cursor.execute("PRAGMA table_info(usuario)")
    cols_user2 = [row[1] for row in cursor.fetchall()]
    if cols_user2 and 'is_owner' not in cols_user2:
        cursor.execute("ALTER TABLE usuario ADD COLUMN is_owner BOOLEAN DEFAULT 0")
        cursor.execute(
            "UPDATE usuario SET is_owner = 1 WHERE id = "
            "(SELECT id FROM usuario WHERE papel = 'admin' ORDER BY id LIMIT 1)"
        )

    # Migracao papel_v1: introduz niveis. Roda uma vez.
    try:
        cursor.execute("CREATE TABLE IF NOT EXISTS migracao_marker (nome VARCHAR(50) PRIMARY KEY, executado_em DATETIME DEFAULT CURRENT_TIMESTAMP)")
        cursor.execute("SELECT 1 FROM migracao_marker WHERE nome='papel_v1'")
        ja_rodou = cursor.fetchone() is not None
        if not ja_rodou:
            cursor.execute("UPDATE usuario SET papel='funcionario' WHERE papel='admin' AND (is_owner IS NULL OR is_owner = 0)")
            cursor.execute("UPDATE usuario SET papel='admin' WHERE is_owner = 1 AND papel <> 'admin'")
            cursor.execute("INSERT INTO migracao_marker (nome) VALUES ('papel_v1')")
    except Exception:
        pass

    # Migração projeto_area.cor
    cursor.execute("PRAGMA table_info(projeto_area)")
    cols_pa = [row[1] for row in cursor.fetchall()]
    if cols_pa and 'cor' not in cols_pa:
        cursor.execute("ALTER TABLE projeto_area ADD COLUMN cor VARCHAR(20)")

    # Migração tarefa_projeto.observacao + recorrencia
    cursor.execute("PRAGMA table_info(tarefa_projeto)")
    cols_tp = [row[1] for row in cursor.fetchall()]
    if cols_tp and 'observacao' not in cols_tp:
        cursor.execute("ALTER TABLE tarefa_projeto ADD COLUMN observacao TEXT")
    if cols_tp and 'recorrencia' not in cols_tp:
        cursor.execute("ALTER TABLE tarefa_projeto ADD COLUMN recorrencia VARCHAR(20)")

    # estoque_producao.nome_pendente
    cursor.execute("PRAGMA table_info(estoque_producao)")
    cols_ep = [row[1] for row in cursor.fetchall()]
    if cols_ep and 'nome_pendente' not in cols_ep:
        cursor.execute("ALTER TABLE estoque_producao ADD COLUMN nome_pendente VARCHAR(200)")

    # estoque_loja.nome_pendente
    cursor.execute("PRAGMA table_info(estoque_loja)")
    cols_el = [row[1] for row in cursor.fetchall()]
    if cols_el and 'nome_pendente' not in cols_el:
        cursor.execute("ALTER TABLE estoque_loja ADD COLUMN nome_pendente VARCHAR(200)")

    # seru_produto_map.fator_quantidade
    cursor.execute("PRAGMA table_info(seru_produto_map)")
    cols_spm = [row[1] for row in cursor.fetchall()]
    if cols_spm and 'fator_quantidade' not in cols_spm:
        cursor.execute("ALTER TABLE seru_produto_map ADD COLUMN fator_quantidade REAL NOT NULL DEFAULT 1.0")

    conn.commit()
    conn.close()

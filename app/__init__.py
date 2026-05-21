import logging
import os

from flask import Flask, Response, render_template, request

from app.extensions import csrf, db, limiter, login_manager, migrate
from app.migrations_legacy import _migrate
from app.utils import agora as agora_brt
from config import Config

logger = logging.getLogger(__name__)


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
        logger.warning('sentry-sdk nao instalada — `pip install sentry-sdk[flask]`')


def create_app(config_class=None):
    _init_sentry()
    app = Flask(__name__)
    app.config.from_object(config_class or Config)

    if not os.environ.get('SECRET_KEY'):
        # Em prod o config.py ja levanta RuntimeError. Aqui so avisa em dev.
        logger.warning('SECRET_KEY nao definida — sessoes expiram a cada restart.')

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    limiter.init_app(app)
    # Flask-Migrate (Alembic). Coexiste com _migrate_postgres/_migrate_sqlite
    # legados ate todas as mudancas futuras de schema irem pela Alembic.
    migrate.init_app(app, db)

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
        import hashlib
        import os
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

    from app.models import MateriaPrima, Receita, TarefaProjeto, Usuario
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

        from app.models import Atribuicao, MateriaPrima, Receita, Usuario

        # Sem queries para usuários não autenticados (ex: página de login)
        if not current_user.is_authenticated:
            return dict(
                sidebar_categorias={}, mp_dict={}, mp_nomes=[],
                receita_nomes=[], funcionarios=[],
            )

        # ── Receitas + categorias (cache 60s) ──
        def _carrega_receitas_globais():
            # defer(imagem_blob/mimetype) — sidebar nao usa essas colunas e elas
            # podem ter 100KB+ cada, estourando memoria do worker.
            from sqlalchemy.orm import defer
            recs = Receita.query.options(
                db.joinedload(Receita.ingredientes),
                defer(Receita.imagem_blob),
                defer(Receita.imagem_mimetype),
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
                'dict': mp_dict,
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
                    from app.utils import hoje as _hoje_brt
                    a = TarefaProjeto.query.filter(
                        TarefaProjeto.prazo.isnot(None),
                        TarefaProjeto.prazo < _hoje_brt(),
                        ~TarefaProjeto.status.in_(['feito', 'cancelado']),
                    ).count()
                    f = TarefaProjeto.query.filter_by(status='fazendo').count()
                    return (a, f)
                proj_atrasadas, proj_fazendo = _cache('proj_count', 10, _carrega_proj_count)
            except Exception:  # noqa: BLE001
                logger.debug('inject_sidebar: falha ao contar projetos', exc_info=True)

        return dict(
            sidebar_categorias=categorias,
            mp_dict=mp_data['dict'],
            mp_nomes=mp_data['nomes'],
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

    @app.route('/manifest.webmanifest')
    def pwa_manifest():
        """PWA manifest na raiz (browsers procuram aqui por convenção)."""
        from flask import send_from_directory
        return send_from_directory(app.static_folder, 'manifest.webmanifest',
                                    mimetype='application/manifest+json')

    @app.route('/sw.js')
    def pwa_service_worker():
        """Service worker precisa estar na raiz para ter escopo / (Service Workers
        só controlam URLs no mesmo path ou abaixo de onde foram registrados)."""
        from flask import send_from_directory
        resp = send_from_directory(app.static_folder, 'sw.js',
                                    mimetype='application/javascript')
        resp.headers['Service-Worker-Allowed'] = '/'
        resp.headers['Cache-Control'] = 'no-cache'
        return resp

    @app.before_request
    def assign_request_id():
        """Atribui ID curto por request pra correlacionar logs.
        Se o cliente mandou X-Request-ID (proxy / load balancer), usa esse."""
        import uuid

        from flask import g
        rid = (request.headers.get('X-Request-ID') or '').strip()
        if not rid or len(rid) > 64:
            rid = uuid.uuid4().hex[:12]
        g.request_id = rid

    @app.after_request
    def add_security_headers(response):
        from flask import g
        response.headers['X-Robots-Tag'] = 'noindex, nofollow'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        if hasattr(g, 'request_id'):
            response.headers['X-Request-ID'] = g.request_id
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            "font-src 'self' https://cdn.jsdelivr.net; "
            "img-src 'self' data:;"
        )
        if request.is_secure:
            response.headers['Strict-Transport-Security'] = (
                'max-age=31536000; includeSubDomains'
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
    from app.blueprints.auth import auth_bp
    from app.blueprints.b2b import b2b_bp
    from app.blueprints.comprovante import comprovante_bp
    from app.blueprints.driver import driver_bp
    from app.blueprints.entregas import entregas_bp
    from app.blueprints.handshake import handshake_bp
    from app.blueprints.main import main_bp
    from app.blueprints.materias_primas import materias_primas_bp
    from app.blueprints.pedidos import pedidos_bp
    from app.blueprints.producao import producao_bp
    from app.blueprints.produtos import produtos_bp
    from app.blueprints.projetos import projetos_bp
    from app.blueprints.receitas import receitas_bp
    from app.blueprints.relatorios import relatorios_bp
    from app.blueprints.rh import rh_bp

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
        _alembic_stamp_se_necessario(app)

        # Seed só roda localmente (SQLite) — em produção os dados já existem
        if app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite'):
            from app.seed import seed_cardapio, seed_database, seed_update_v2
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
    """Cria usuário admin padrão se não existir nenhum.

    Se ADMIN_PASSWORD nao estiver no env, gera senha aleatoria e a
    imprime no log uma unica vez (no primeiro start). Anote no momento —
    nao ha como recuperar depois sem reset manual.
    """
    from app.models import Usuario
    if not Usuario.query.filter_by(papel='admin').first():
        senha_env = os.environ.get('ADMIN_PASSWORD')
        if senha_env:
            senha = senha_env
        else:
            import secrets as _secrets
            senha = _secrets.token_urlsafe(16)
            logger.warning('=' * 60)
            logger.warning('ADMIN criado com senha aleatoria: %s', senha)
            logger.warning('ANOTE AGORA — nao sera mostrada de novo.')
            logger.warning('Pra controlar, defina ADMIN_PASSWORD no env antes do 1o start.')
            logger.warning('=' * 60)
        admin = Usuario(nome='Admin', login='admin', papel='admin', is_owner=True)
        admin.set_senha(senha)
        db.session.add(admin)
        db.session.commit()


def _alembic_stamp_se_necessario(app):
    """Sincroniza Alembic com o banco no startup.

    Duas fases:
    1) Stamp baseline (uma vez): se ha tabela `usuario` mas nao
       `alembic_version`, eh o primeiro startup apos adocao do Alembic
       em schema legado. Marca como ja estando na baseline.
    2) Upgrade (sempre): aplica migrations pendentes. Se Alembic ja esta
       na head, eh no-op rapido. Se houve migration nova (ex: B9 SeruDebitoMov),
       cria a tabela aqui em prod.

    Em testes (SQLite in-memory), `db.create_all()` ja deixou tudo no
    estado mais novo — `upgrade` detecta que esta na head e nao faz nada.

    Idempotente. Race-safe em multi-worker porque Alembic usa UPDATE
    atomico do `alembic_version` dentro de transacao.
    """
    from sqlalchemy import inspect
    try:
        insp = inspect(db.engine)
        tabelas = set(insp.get_table_names())
        if 'usuario' not in tabelas:
            return  # banco novo/vazio
        # Fase 1: stamp baseline se Alembic nunca foi inicializado
        if 'alembic_version' not in tabelas:
            from flask_migrate import stamp
            stamp(directory='migrations', revision='head')
            logger.warning(
                'Alembic: baseline marcada (stamp head) — schema legado adotado.'
            )
            return  # primeira execucao: nao roda upgrade alem do stamp
        # Fase 2: aplica migrations pendentes
        from flask_migrate import upgrade as _upgrade
        _upgrade(directory='migrations')
    except Exception:  # noqa: BLE001
        logger.exception(
            'Alembic stamp/upgrade falhou. Verificar manualmente com '
            '`railway run flask db current` e `flask db upgrade`.'
        )



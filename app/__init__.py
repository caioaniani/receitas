import logging
import os

from flask import Flask, flash, jsonify, redirect, render_template, request
from flask_wtf.csrf import CSRFError

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

    # ProxyFix: confia nos headers X-Forwarded-* do proxy do Railway (1 hop).
    # Sem isso, `request.is_secure`/`request.remote_addr` ficam falsos atras
    # do proxy → HSTS nao era enviado, rate-limit-por-IP via no IP do proxy
    # (todos os clientes iguais). Encontrado 23/06/2026.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # Cookies de sessao: Secure (so via HTTPS), HttpOnly (JS nao le) e
    # SameSite=Lax (browser nao envia cookie em POST cross-site → barra
    # CSRF de form em outro dominio). Em dev local (HTTP), Secure
    # impediria login → so liga em prod.
    if not app.config.get('TESTING') and not os.environ.get('PYTEST_RUNNING'):
        app.config.setdefault('SESSION_COOKIE_SECURE', True)
    app.config.setdefault('SESSION_COOKIE_HTTPONLY', True)
    app.config.setdefault('SESSION_COOKIE_SAMESITE', 'Lax')

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
    def inject_ui_v2():
        # Interface v2 (ver app/ui_v2.py): o base.html troca o shell
        # (CSS + sidebar) por esta variavel — nunca ler config direto no
        # template, o cookie de "voltar ao anterior" mora no helper.
        from app.ui_v2 import ui_v2_ativo
        return {'ui_v2': ui_v2_ativo()}

    @app.context_processor
    def inject_static_version():
        """Versionamento de arquivos estaticos para cache busting.
        Usa hash MD5 do conteudo (8 chars) — muda sempre que arquivo muda,
        independente de mtime (Railway deploy nao preserva mtime original)."""
        import hashlib
        import os
        versions = {}
        # LOJA inclusa (26/07/2026): os assets da loja publica ficavam de FORA
        # deste versionamento e o <link>/<script> iam sem `?v=`. Com o service
        # worker fazendo cache-first em /static, o navegador do cliente
        # continuava servindo o loja.css VELHO depois do deploy — o HTML novo
        # (miniaturas da galeria) chegava sem as regras que as dimensionam e a
        # pagina do produto ESTOUROU com fotos em tamanho natural (relatado
        # pelo dono). Sem `?v=` a correcao de CSS so chegaria por hard-refresh.
        for rel in ('js/projetos.js', 'js/app.js', 'js/entregas.js',
                    'js/pdv_mapeamento.js', 'css/style.css',
                    'js/home-v2.js', 'css/ui-v2.css', 'css/industria-v2.css',
                    'css/treino-v2.css',
                    'loja/loja.css', 'loja/carrinho.js', 'loja/checkout.js'):
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

    from app.models import MateriaPrima, Produto, Receita, TarefaProjeto, Usuario
    _MODELOS_CACHEADOS = (Receita, MateriaPrima, Produto, Usuario, TarefaProjeto)

    @_sa_event.listens_for(db.session, 'before_commit')
    def _invalidate_on_change(session):
        alvo = _MODELOS_CACHEADOS
        if any(isinstance(o, alvo) for o in session.new) \
                or any(isinstance(o, alvo) for o in session.dirty) \
                or any(isinstance(o, alvo) for o in session.deleted):
            _SIDEBAR_CACHE.clear()

    @app.context_processor
    def inject_historico_humano():
        """Expoe rotulos amigaveis (tipos de mov, etapas de handshake, etc.)
        pra qualquer template — evita duplicar dicts inline em cada .html."""
        from app.services import historico_humano
        return {'historico_humano': historico_humano}

    @app.context_processor
    def inject_sidebar():
        from flask_login import current_user

        from app.models import Atribuicao, MateriaPrima, Receita, Usuario

        # Sem queries para usuários não autenticados (ex: página de login)
        if not current_user.is_authenticated:
            return dict(
                sidebar_categorias={}, mp_info={}, mp_nomes=[],
                receita_nomes=[], produto_nomes=[], funcionarios=[],
            )

        # ── Receitas + categorias (cache 60s) ──
        def _carrega_receitas_globais():
            # defer(imagem_blob/mimetype) — sidebar nao usa essas colunas e elas
            # podem ter 100KB+ cada, estourando memoria do worker.
            from sqlalchemy.orm import defer
            # Arquivadas ficam fora da sidebar e dos datalists (selecao de
            # uso ativo) — historico/telas de registro nao passam por aqui.
            recs = Receita.query.options(
                db.joinedload(Receita.ingredientes),
                defer(Receita.imagem_blob),
                defer(Receita.imagem_mimetype),
            ).filter(Receita.arquivada_em.is_(None)
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
            mps = MateriaPrima.ativas().order_by(MateriaPrima.nome).all()
            mp_info = {mp.nome: {'custo_por_kg': mp.custo_por_kg, 'unidade': mp.unidade,
                                  'peso_unidade': mp.peso_unidade} for mp in mps}
            return {
                'info': mp_info,
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

        # ── Cestas orfas (cache 60s, admin+owner) ──
        # Sinal critico: ProdutoItem com FK NULL nao baixa estoque na venda
        # da cesta. Banner global em base.html consome este contador.
        cestas_orfaos_count = 0
        if current_user.is_admin():
            try:
                def _carrega_cestas_orfaos():
                    from app.services.cestas import contar_produto_itens_orfaos
                    return contar_produto_itens_orfaos()
                cestas_orfaos_count = _cache('cestas_orfaos', 60, _carrega_cestas_orfaos)
            except Exception:  # noqa: BLE001
                logger.debug('inject_sidebar: falha ao contar cestas orfas', exc_info=True)

        # ── Produtos nomes (cache 60s, pra datalist em criar cesta) ──
        def _carrega_produto_nomes():
            from app.models import Produto
            return [p.nome for p in Produto.query
                    .filter(Produto.ativo.is_(True))
                    .order_by(Produto.nome).all()]
        produto_nomes = _cache('produto_nomes', 60, _carrega_produto_nomes)

        return dict(
            sidebar_categorias=categorias,
            mp_info=mp_data['info'],
            mp_nomes=mp_data['nomes'],
            receita_nomes=receita_nomes,
            produto_nomes=produto_nomes,
            funcionarios=funcionarios,
            proj_atrasadas=proj_atrasadas,
            proj_fazendo=proj_fazendo,
            cestas_orfaos_count=cestas_orfaos_count,
        )

    # NOTA: /robots.txt foi REMOVIDO daqui (22/06/2026). Antes devolvia
    # Disallow: / fixo, o que vazava pro Search Console ("sitemap em HTML")
    # quando a loja virou publica. Agora /robots.txt eh servido por
    # `main_bp.routes.robots_root` (alias do robots da loja), que respeita
    # `LOJA_VISIVEL`: visivel -> Allow + aponta o Sitemap; em teste ->
    # Disallow. Decisao de mover do app/__init__.py pro blueprint:
    # centraliza a politica de SEO em UM lugar.

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
    def _redireciona_para_https():
        """Defesa em profundidade: se a request chegar via HTTP em prod
        (proxy mal configurado, rota nova esquecida), redireciona pra HTTPS.
        Hoje a Railway força HTTPS por config dela; isso não depende disso.
        Usa o X-Forwarded-Proto via ProxyFix; em dev (localhost) e em testes
        não age (testes usam test_client em HTTP por design)."""
        if app.config.get('TESTING') or os.environ.get('PYTEST_RUNNING'):
            return None
        host = (request.host or '').split(':')[0].lower()
        if host in ('localhost', '127.0.0.1', '0.0.0.0'):
            return None
        if request.scheme == 'https':
            return None
        # GET/HEAD redireciona; POST com HTTP é abortado (não reenviar
        # corpo pra outra URL — atacante poderia ter capturado credenciais).
        if request.method not in ('GET', 'HEAD'):
            return ('HTTPS required', 400)
        return redirect(request.url.replace('http://', 'https://', 1),
                        code=301)

    @app.before_request
    def _sessao_permanente():
        """Sessao PERMANENTE (PERMANENT_SESSION_LIFETIME, 30d rolando).

        Sem isto o cookie de sessao e "de navegador" e morre ao fechar/reciclar
        a aba; o remember-me (1 ano) reloga numa sessao NOVA e o token CSRF da
        pagina que ja estava aberta para de bater — o usuario leva "Sessao de
        seguranca expirada" ao enviar o form (caso real 25/07/2026: subir foto
        na ficha da receita). Nao afrouxa acesso: quem estava logado ja seguia
        logado pelo remember-me."""
        from flask import session
        session.permanent = True

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

    @app.before_request
    def _roteamento_por_host():
        """Separa a LOJA (opao.online) do ADMIN (gestao.*).

        Em hosts de loja (config LOJA_HOSTS): só `/loja/*` + assets respondem;
        a raiz `/` redireciona pra `/loja/`; QUALQUER rota de admin/gestão
        (login, /admin, /pedidos, etc.) vira 404 — o cliente nunca encontra a
        tela de gestão. Em gestao.* (e qualquer outro host) nada muda: o
        sistema responde inteiro como sempre.

        Decisão do dono 18/06/2026: opao.online é só o site público."""
        from flask import abort
        host = (request.host or '').split(':')[0].lower()

        # Cutover: domínio antigo (VNDA) → 302 pro site novo. Chave liga/desliga
        # via env SITE_REDIRECT_HOSTS (vazio = inerte). Manda tudo pra raiz do
        # destino (paths do VNDA não existem aqui — evita 404). 302 (não 301)
        # pra poder CORTAR o redirecionamento sem cache grudado no navegador.
        redirect_hosts = {h.strip().lower()
                          for h in (app.config.get('SITE_REDIRECT_HOSTS') or '')
                          .split(',') if h.strip()}
        if host in redirect_hosts:
            # NUNCA redirecionar paths essenciais pra crawlers/integracoes
            # (Google Search Console, validacao Apple Pay, robots/sitemap),
            # senao ferra SEO e merchant validation. Bug 22/06/2026: env
            # ficou setada apos cutover, sitemap.xml virou 302 -> opao.online
            # e Search Console acusava "sitemap em HTML".
            p = request.path
            essenciais = (p in ('/sitemap.xml', '/robots.txt',
                                '/favicon.ico', '/health')
                          or p.startswith('/.well-known/')
                          or p.startswith('/static/'))
            if not essenciais:
                destino = (app.config.get('SITE_REDIRECT_DESTINO')
                           or 'https://opao.online').rstrip('/')
                return redirect(destino + '/', code=302)

        from app.utils import hosts_loja
        hosts = hosts_loja()
        if host not in hosts:
            return None  # gestao.* / railway.app / outros: comportamento full
        p = request.path
        # Liberados no host da loja: a propria loja + assets + infra basica.
        # /sitemap.xml e /robots.txt na RAIZ sao padrao Google/RFC — crawlers
        # buscam ali; sem isso vira 404 HTML e o Search Console acusa
        # "sitemap em HTML". A rota mora em /loja/sitemap.xml mas Google
        # espera /sitemap.xml — servimos os dois (alias top-level).
        if (p == '/loja' or p.startswith('/loja/')
                or p.startswith('/static/')
                or p in ('/health', '/favicon.ico',
                         '/sitemap.xml', '/robots.txt')):
            return None
        if p == '/':
            return redirect('/loja/', code=302)
        abort(404)

    @app.before_request
    def _gate_conta():
        """Trava por conta (23/07/2026, decisão do dono):
        - `senha_provisoria`: força trocar a senha no 1º acesso antes de
          liberar qualquer tela (a senha veio no e-mail; a pessoa sabe a atual).
        - `somente_treino`: acesso restrito à área de treinamento (/treino);
          o resto vira redirect (barra por URL também, não só escondendo link).
        Roda depois do roteamento por host; `getattr` defensivo enquanto a
        coluna propaga. Allowlist evita loop (a própria troca, sair, estáticos).
        """
        from flask import url_for
        from flask_login import current_user
        if not getattr(current_user, 'is_authenticated', False):
            return None
        ep = request.endpoint or ''
        if not ep:
            return None  # URL sem rota → deixa 404 (não redireciona asset/404)
        liberados = {'auth.minha_senha', 'auth.logout', 'auth.csrf_token_novo',
                     'static', 'pwa_service_worker', 'pwa_manifest', 'health'}
        if ep in liberados or ep.endswith('.static'):
            return None
        if getattr(current_user, 'senha_provisoria', False):
            return redirect(url_for('auth.minha_senha'))
        if (getattr(current_user, 'somente_treino', False)
                and not ep.startswith('treino.')):
            return redirect(url_for('treino.home'))
        return None

    @app.after_request
    def add_security_headers(response):
        import os

        from flask import g

        from app.utils import host_atual_eh_loja
        # noindex global protege o admin. So sai (deixa indexar) quando a
        # vitrine esta publica (LOJA_VISIVEL=1, Fase 8) E o host e o dominio
        # publico da loja (opao.online). No gestao.*/loja o noindex CONTINUA:
        # e a mesma loja servida pelo dominio de gestao, e indexar os dois
        # geraria conteudo duplicado pro Google. Em modo teste (=0) tambem
        # continua, pra nao vazar pra busca por engano.
        loja_publica = (
            os.environ.get('LOJA_VISIVEL', '0').strip() == '1'
            and request.path.startswith('/loja')
            and host_atual_eh_loja()
        )
        if not loja_publica:
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
            "img-src 'self' data: https://*.dropbox.com "
            "https://*.dropboxusercontent.com;"
        )
        # Tela do padeiro: Web Playback SDK do Spotify (15/07/2026 — decisao
        # do dono: a PROPRIA tela toca a musica, nao outro aparelho). O SDK
        # exige script do sdk.scdn.co, WebSocket/HTTPS pros dominios do
        # Spotify e audio via MSE (blob:). Capa do album vem do *.scdn.co.
        # ESCOPADO ao /padeiro — o resto do app segue com a CSP estrita.
        if request.path.startswith('/padeiro'):
            # Dominios de AUDIO do Spotify variam por regiao/CDN (scdn.co,
            # spotifycdn.com/.net, Akamai) — lista curta demais = a musica
            # toca o 1º buffer (~10s) e MORRE com o estado "tocando" (caso
            # real 15/07). O report-uri manda cada bloqueio pro nosso
            # endpoint, visivel na sonda /api/claude/spotify-debug — se um
            # host novo aparecer, o relatorio diz QUAL.
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; "
                "script-src 'self' https://cdn.jsdelivr.net "
                "https://sdk.scdn.co 'unsafe-inline'; "
                "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                "font-src 'self' https://cdn.jsdelivr.net; "
                "connect-src 'self' https://*.spotify.com "
                "wss://*.spotify.com https://*.scdn.co "
                "https://*.spotifycdn.com https://*.spotifycdn.net "
                "https://*.akamaized.net; "
                "media-src 'self' blob: https://*.spotifycdn.com "
                "https://*.spotifycdn.net https://*.akamaized.net "
                "https://*.scdn.co; "
                "worker-src 'self' blob:; "
                "frame-src 'self' https://sdk.scdn.co; "
                "img-src 'self' data: https://*.dropbox.com "
                "https://*.dropboxusercontent.com https://*.scdn.co "
                "https://*.spotifycdn.com; "
                "report-uri /padeiro/csp-report;"
            )
        # Treinamento gamificado (/treino): afrouxa a CSP só nesta área pro
        # player do Cloudflare Stream (iframe + SDK). O resto segue estrita.
        if request.path.startswith('/treino'):
            # Cloudflare Stream: o player é um iframe (frame-src), o SDK do
            # player carrega de embed.cloudflarestream.com (script-src), e o
            # upload DIRETO do navegador vai pro host de ingest do Cloudflare
            # (connect-src). O byte não passa pelo nosso servidor.
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; "
                "script-src 'self' https://cdn.jsdelivr.net "
                "https://embed.cloudflarestream.com 'unsafe-inline'; "
                "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                "font-src 'self' https://cdn.jsdelivr.net; "
                "media-src 'self' blob:; "
                "frame-src https://*.cloudflarestream.com; "
                "connect-src 'self' https://upload.videodelivery.net "
                "https://*.cloudflarestream.com; "
                "img-src 'self' data: https://*.dropbox.com "
                "https://*.dropboxusercontent.com https://*.cloudflarestream.com;"
            )
        # Popup do painel de entregas: o detalhe do pedido (?embed=1) e embutido
        # num iframe de MESMA ORIGEM (gestao.*). X-Frame-Options=DENY bloquearia
        # ate o same-origin — troca por SAMEORIGIN + frame-ancestors 'self'
        # (cross-origin/clickjacking segue bloqueado). Escopado ao detalhe do
        # pedido pra nao afrouxar o resto do admin.
        # Embutidos same-origin com ?embed=1: detalhe do pedido + o painel de
        # pedidos (v1, em /entregas/painel-testes apos o swap de 26/06; tambem
        # /entregas/painel pra cobrir transicao — inocuo, so libera same-origin).
        # `request.args` (query string), NUNCA `request.values`: values inclui o
        # FORM, e ler o form aqui re-dispara o parse do corpo — num upload acima
        # do MAX_CONTENT_LENGTH isso levantava RequestEntityTooLarge DENTRO do
        # after_request, ou seja, DEPOIS da resposta amigavel de 413 ja pronta,
        # virando erro cru (achado 25/07/2026). O `?embed=1` sempre foi query
        # param (base.html usa request.args). Path primeiro: curto-circuito
        # barato, sem tocar em nada da request.
        if ((request.path.startswith('/admin/loja-online/pedidos')
                or request.path == '/entregas/painel'
                or request.path == '/entregas/painel-testes')
                and request.args.get('embed')):
            response.headers['X-Frame-Options'] = 'SAMEORIGIN'
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; "
                "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                "font-src 'self' https://cdn.jsdelivr.net; "
                "img-src 'self' data: https://*.dropbox.com "
                "https://*.dropboxusercontent.com; "
                "frame-ancestors 'self';"
            )
        # Painel OFICIAL (v2, /entregas/painel): pedidos (iframe same-origin do
        # v1 via /painel-testes?embed=1) + atendimento NOSSO (lista/thread do
        # Chatwoot via API, sem iframe). frame-src 'self' (so o iframe do v1).
        # img-src libera o dominio do Chatwoot pros ANEXOS de imagem dos
        # clientes na thread. (O v1 standalone em /painel-testes usa o CSP
        # default — o mesmo que tinha quando era o /painel.)
        if request.path == '/entregas/painel':
            chatwoot = (app.config.get('CHATWOOT_URL') or '').strip().rstrip('/')
            img_cw = f' {chatwoot}' if chatwoot else ''
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; "
                "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                "font-src 'self' https://cdn.jsdelivr.net; "
                "img-src 'self' data: https://*.dropbox.com "
                f"https://*.dropboxusercontent.com{img_cw}; "
                "frame-src 'self';"
            )
        # Excecao: o card do CRM (/crm/card) e embutido como iframe DENTRO do
        # Chatwoot (outro dominio). X-Frame-Options=DENY bloquearia; e
        # ALLOW-FROM nao whitelista cross-origin de forma confiavel. Usamos
        # CSP frame-ancestors com a URL do Chatwoot. Sem CHATWOOT_URL setado,
        # mantemos DENY (card inutilizavel ate configurar).
        if request.path.startswith('/crm/card'):
            chatwoot = (app.config.get('CHATWOOT_URL') or '').strip().rstrip('/')
            if chatwoot:
                response.headers.pop('X-Frame-Options', None)
                response.headers['Content-Security-Policy'] = (
                    "default-src 'self'; "
                    "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                    "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                    "img-src 'self' data: https://*.dropbox.com "
                    "https://*.dropboxusercontent.com; "
                    f"frame-ancestors 'self' {chatwoot};"
                )
        # Loja online: o cartão é tokenizado no navegador pelo SDK do
        # Pagar.me (checkout.pagar.me) com fetch direto pra api.pagar.me —
        # PCI baixo (o número do cartão nunca toca nosso servidor). Pra isso
        # o CSP precisa liberar esse script + connect. QR do Pix é data-URI
        # (já coberto por img-src data:).
        if request.path.startswith('/loja'):
            # Chatwoot widget na loja: precisa permitir o sdk.js, o
            # WebSocket de realtime (ActionCable), iframe interna do widget e
            # avatares dos atendentes. Só libera se o widget estiver ligado
            # (CHATWOOT_WEBSITE_TOKEN setado) — senão mantém o CSP fechado.
            cw_token = (app.config.get('CHATWOOT_WEBSITE_TOKEN') or '').strip()
            cw_origin = ((app.config.get('CHATWOOT_PUBLIC_URL') or '')
                         .strip().rstrip('/'))
            cw_ws = cw_origin.replace('https://', 'wss://') if cw_origin else ''
            cw_extra_script = f' {cw_origin}' if cw_token and cw_origin else ''
            cw_extra_connect = (f' {cw_origin} {cw_ws}'
                                if cw_token and cw_origin else '')
            cw_extra_frame = f' {cw_origin}' if cw_token and cw_origin else ''
            cw_extra_img = f' {cw_origin}' if cw_token and cw_origin else ''
            cw_extra_style = f' {cw_origin}' if cw_token and cw_origin else ''
            # Analytics/marketing (GA4 + Meta Pixel): só afrouxa o CSP quando a
            # env var existe (sem tracking, CSP segue fechado). Sem estes
            # dominios o navegador BLOQUEIA o gtag.js e o fbevents.js e os
            # eventos nunca saem (bug 23/06/2026: Pixel "eventos nunca
            # recebidos" porque connect.facebook.net não estava no script-src).
            an_script = an_img = an_connect = ''
            if (app.config.get('GA4_ID') or '').strip():
                an_script += ' https://www.googletagmanager.com'
                an_img += (' https://www.google-analytics.com'
                           ' https://*.google-analytics.com')
                an_connect += (' https://www.googletagmanager.com'
                               ' https://www.google-analytics.com'
                               ' https://*.google-analytics.com'
                               ' https://*.analytics.google.com')
            if (app.config.get('META_PIXEL_ID') or '').strip():
                an_script += ' https://connect.facebook.net'
                an_img += ' https://www.facebook.com https://*.facebook.com'
                an_connect += (' https://connect.facebook.net'
                               ' https://www.facebook.com')
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; "
                f"script-src 'self' https://cdn.jsdelivr.net "
                f"https://checkout.pagar.me{cw_extra_script}{an_script} "
                "'unsafe-inline'; "
                f"style-src 'self' https://cdn.jsdelivr.net{cw_extra_style} "
                "'unsafe-inline'; "
                "font-src 'self' https://cdn.jsdelivr.net; "
                f"img-src 'self' data: https://*.dropbox.com "
                f"https://*.dropboxusercontent.com{cw_extra_img}{an_img}; "
                f"connect-src 'self' https://api.pagar.me"
                f"{cw_extra_connect}{an_connect}; "
                f"frame-src https://checkout.pagar.me{cw_extra_frame};"
            )
        # HSTS: força HTTPS no navegador por 1 ano. Liga em qualquer host
        # exceto localhost (dev). Antes era `if request.is_secure`, mas atrás
        # do proxy do Railway esse flag era False → HSTS NUNCA saía em prod.
        # Agora com ProxyFix funciona; ligar sempre fora de dev é defesa em
        # profundidade caso o ProxyFix algum dia falhe.
        host = (request.host or '').split(':')[0].lower()
        if host not in ('localhost', '127.0.0.1', '0.0.0.0'):
            response.headers['Strict-Transport-Security'] = (
                'max-age=31536000; includeSubDomains'
            )
        # Permissions-Policy: nega APIs sensíveis que o site não usa
        # (mitiga XSS escalando pra microfone/câmera/GPS do cliente).
        politica_da_rota = response.headers.get('Permissions-Policy')
        politica_padrao = (
            'camera=(), microphone=(), geolocation=(), payment=(self), '
            'usb=(), magnetometer=(), gyroscope=(), accelerometer=()'
        )
        # Algumas telas podem precisar de uma restrição adicional. A aula de
        # treinamento no celular, por exemplo, nega fullscreen para a pergunta
        # do checkpoint nunca ficar atrás do player nativo.
        response.headers['Permissions-Policy'] = (
            f'{politica_padrao}, {politica_da_rota}'
            if politica_da_rota else politica_padrao
        )
        # Cache agressivo para assets estaticos (CSS/JS/fonts/imagens)
        if request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'public, max-age=86400, stale-while-revalidate=604800'
        return response

    # ── Error handlers ──
    @app.errorhandler(CSRFError)
    def csrf_error(e):
        # Com WTF_CSRF_TIME_LIMIT=None (config.py) o token nao vence mais por
        # tempo — isto so dispara no residual: sessao trocada (re-login em
        # outra aba), cookie apagado, ou POST cross-site de verdade. O POST
        # NUNCA executou; aqui so muda o formato da falha.
        # Autosaves via fetch (X-CSRFToken) mandam JSON: devolvemos JSON pra o
        # front distinguir "token de seguranca invalido" de erro real — ele
        # busca token novo em /auth/csrf-token e re-tenta em vez de falhar
        # com alert criptico.
        # DIAGNÓSTICO (25/07/2026): o upload de foto da ficha continuou dando
        # "sessão expirada" mesmo com a sessão permanente. A causa real está no
        # `description` do Flask-WTF ("token is missing" = campo não chegou no
        # corpo, ex. multipart truncado; "do not match"/"session token is
        # missing" = cookie de sessão) — sem ela ficamos adivinhando. Loga o
        # contexto e devolve um código curto pro usuário conseguir reportar.
        motivo = str(getattr(e, 'description', '') or '')[:120]
        campos = 0
        try:                       # ler o form aqui pode reestourar (413)
            campos = len(request.form)
        except Exception:          # noqa: BLE001
            campos = -1
        logger.warning(
            'CSRF falhou: motivo=%r path=%s ct=%s len=%s campos=%s '
            'tem_cookie_sessao=%s',
            motivo, request.path, request.content_type,
            request.content_length, campos,
            bool(request.cookies.get(app.config.get('SESSION_COOKIE_NAME',
                                                    'session'))))
        if request.is_json:
            return jsonify(ok=False, erro='csrf_expirada', motivo=motivo,
                           msg='Sessão de segurança expirada — recarregue a '
                               'página e tente de novo.'), 400
        # Form HTML: volta pra tela de origem com aviso + token novo, no lugar
        # da pagina "400 Bad Request" crua (caso real 02/07/2026 no
        # /telaindustriateste/enviar). So redireciona pra referrer da MESMA
        # origem — referrer externo (ataque cross-site) cai na home.
        from urllib.parse import urlparse
        ref = request.referrer or ''
        destino = ref if urlparse(ref).netloc == request.host else '/'
        # ENVIO CORTADO NO MEIO (upload) != sessão expirada. Num POST multipart
        # que chega SEM NENHUM campo, o corpo não completou: a conexão caiu no
        # meio da subida do arquivo. O Werkzeug devolve form vazio em SILÊNCIO
        # (get_input_stream sem tamanho -> stream vazio; FormDataParser é
        # silent=True), e o Flask-WTF só consegue dizer "token ausente" — o que
        # virava a mensagem ENGANOSA de sessão expirada e fez o dono perseguir
        # sessão/cookie por horas (25/07/2026, foto de 12MB da fototeca no 4G).
        # Aqui a mensagem passa a dizer a verdade e o que fazer.
        multipart = 'multipart/form-data' in (request.content_type or '')
        if multipart and campos == 0:
            flash('O envio da foto não completou — a conexão caiu no meio da '
                  'subida (arquivo grande / sinal fraco). Tente de novo: a '
                  'foto agora é reduzida automaticamente antes de subir.',
                  'warning')
            return redirect(destino)
        # Demais casos: CSRF de verdade. O código entre colchetes diz SE o token
        # não chegou no corpo ou SE o cookie de sessão falhou.
        curto = ('token-ausente' if 'missing' in motivo.lower()
                                    and 'session' not in motivo.lower()
                 else 'sessao-ausente' if 'session' in motivo.lower()
                 else 'token-nao-bate' if 'match' in motivo.lower()
                 else 'outro')
        flash('Sessão de segurança expirada — a página foi recarregada com um '
              f'código novo. Tente de novo. [{curto} · {campos} campos]',
              'warning')
        return redirect(destino)

    @app.errorhandler(413)
    def arquivo_grande(e):
        """Upload acima do MAX_CONTENT_LENGTH. Sem este handler o usuario via a
        pagina crua "Request Entity Too Large" e nao entendia que o problema era
        o TAMANHO da foto (celular novo tira foto de 20MB+)."""
        limite = app.config.get('MAX_CONTENT_LENGTH') or 0
        mb = int(limite / (1024 * 1024)) if limite else '?'
        msg = (f'Arquivo grande demais (o limite e {mb} MB). '
               'Tire uma foto menor ou reduza a imagem antes de enviar.')
        if request.is_json:
            return jsonify(ok=False, erro='arquivo_grande', msg=msg), 413
        from urllib.parse import urlparse
        ref = request.referrer or ''
        destino = ref if urlparse(ref).netloc == request.host else '/'
        flash(msg, 'warning')
        return redirect(destino)

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
    from app.blueprints.lalamove import lalamove_bp
    from app.blueprints.loja import loja_bp
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
    app.register_blueprint(lalamove_bp)
    app.register_blueprint(driver_bp, url_prefix='/driver')
    app.register_blueprint(comprovante_bp, url_prefix='/entrega')
    app.register_blueprint(loja_bp, url_prefix='/loja')
    app.register_blueprint(projetos_bp)
    from app.blueprints.pdv import pdv_bp
    app.register_blueprint(pdv_bp, url_prefix='/pdv')
    from app.blueprints.bot import bot_bp
    app.register_blueprint(bot_bp)
    # API read-only do assistente (Claude Code): leituras de producao via
    # token CLAUDE_API_TOKEN. Sem a env, as rotas respondem 503.
    from app.blueprints.claude_api import claude_api_bp
    # CSRF isento: a autenticacao aqui e Bearer (CLAUDE_API_TOKEN), nao cookie
    # — CSRF protege sessao por cookie e nao se aplica. Mesmo padrao do
    # wifi_api abaixo. Necessario pro POST de diagnostico (/echo-upload).
    csrf.exempt(claude_api_bp)
    app.register_blueprint(claude_api_bp, url_prefix='/api/claude')
    # API de autenticacao do Wi-Fi das lojas (RADIUS). A ponte RADIUS
    # (wifi_radius/bridge.py, roda num VPS) chama POST /api/wifi/radius-check
    # server-to-server com Bearer WIFI_RADIUS_TOKEN — CSRF isento como as
    # demais APIs de token da casa. Sem a env, responde 503.
    from app.blueprints.wifi_api import wifi_api_bp
    csrf.exempt(wifi_api_bp)
    app.register_blueprint(wifi_api_bp, url_prefix='/api/wifi')
    # Copilot WEB desativado em 10/06/2026 — decisao do dono: usar so o
    # Slack bot (servico copilot.py) e o bot pessoal no WhatsApp (bot_bp).
    from app.blueprints.fornecedores import fornecedores_bp
    app.register_blueprint(fornecedores_bp)
    from app.blueprints.contas_pagar import contas_pagar_bp
    app.register_blueprint(contas_pagar_bp)
    from app.blueprints.cobrancas import cobrancas_bp
    app.register_blueprint(cobrancas_bp, url_prefix='/cobrancas')
    from app.blueprints.lista_compras import lista_compras_bp
    app.register_blueprint(lista_compras_bp)
    from app.blueprints.notificacoes import notificacoes_bp
    app.register_blueprint(notificacoes_bp)
    from app.blueprints.slack import slack_bp
    app.register_blueprint(slack_bp)
    from app.blueprints.zapi_bot import zapi_bot_bp
    app.register_blueprint(zapi_bot_bp)
    from app.blueprints.padeiro import padeiro_bp
    app.register_blueprint(padeiro_bp, url_prefix='/padeiro')
    from app.blueprints.patrimonio import patrimonio_bp
    app.register_blueprint(patrimonio_bp, url_prefix='/patrimonio')
    from app.blueprints.checklist import checklist_bp
    app.register_blueprint(checklist_bp, url_prefix='/checklist')

    from app.blueprints.precadastro import precadastro_bp
    app.register_blueprint(precadastro_bp)
    from app.blueprints.industria_teste import industria_teste_bp
    app.register_blueprint(industria_teste_bp, url_prefix='/telaindustriateste')
    from app.blueprints.avisos import avisos_bp
    app.register_blueprint(avisos_bp)
    from app.blueprints.notas import notas_bp
    app.register_blueprint(notas_bp)

    from app.blueprints.treino import treino_bp
    app.register_blueprint(treino_bp)
    from app.blueprints.crm import crm_bp
    app.register_blueprint(crm_bp)

    # Ativa audit log (listeners SQLAlchemy)
    from app.services.audit import init_audit
    init_audit()

    with app.app_context():
        _setup_schema(app)

        # Seeds populam catalogo/RH/projetos no startup. Em teste o conftest
        # faz drop_all+create_all logo apos create_app(), descartando tudo isso
        # — semear so desperdicaria ~3s por teste (×374 = ~19min de suite). Pula
        # via PYTEST_RUNNING; cada teste usa as proprias fixtures. Producao nunca
        # seta PYTEST_RUNNING, entao o comportamento la fica inalterado.
        if not os.environ.get('PYTEST_RUNNING'):
            # Seed só roda localmente (SQLite) — em produção os dados já existem
            if app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite'):
                from app.seed import seed_cardapio, seed_database, seed_update_v2
                seed_database()
                seed_cardapio()
                seed_update_v2()

            # Produtos do site — roda em todos os ambientes (SQLite + PostgreSQL)
            from app.seed import seed_cestas_categoria, seed_site_products
            seed_site_products()
            # Normaliza a categoria das cestas (libera cartinha no checkout)
            seed_cestas_categoria()

            # RH: lojas + funcionários — roda em todos os ambientes
            from app.seed import seed_rh, seed_rh_escala
            seed_rh()
            seed_rh_escala()

            # Gestão de Projetos — seed inicial em todos os ambientes
            from app.seed import seed_projetos
            seed_projetos()

            # Catalogo da Lista de Compras semanal por loja — idempotente.
            from app.seed import seed_lista_compras
            seed_lista_compras()

        _criar_admin()

    # Cron de auto-sync Seru → EstoqueLoja (15min). Roda dentro de
    # cada worker gunicorn mas usa pg_try_advisory_lock pra deduplicate.
    # Em teste o conftest seta TESTING só DEPOIS de create_app(), então o
    # scheduler escaparia o guard acima e um job VNDA dispararia no meio da
    # suite (mutando estado de modulo, deixando test_pdv_saude flaky). Pula
    # via PYTEST_RUNNING, mesmo padrao de migrations_legacy.py.
    if not app.config.get('TESTING') and not os.environ.get('PYTEST_RUNNING'):
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


def _setup_schema(app):
    """Cria tabelas + roda migrations no startup, serializado entre workers.

    Os multiplos workers do gunicorn rodam isto ao mesmo tempo no boot.
    `db.create_all()` (checkfirst=True) tem CORRIDA ao adicionar tabela
    NOVA: dois workers consultam `pg_class`, ambos veem a tabela faltando,
    ambos disparam `CREATE TABLE` -> o segundo bate em 'duplicate key
    pg_type_typname_nsp_index' (UniqueViolation). Sintoma real: deploy da
    Fase 3 (tabela `cliente`) gerou IntegrityError no Sentry; auto-curava
    no restart do worker, mas sujo e recorrente a cada tabela nova.

    Fix canonico: advisory lock BLOQUEANTE (nao `try`) serializa o setup.
    O primeiro worker cria; os demais ESPERAM e, ao entrar, `create_all`
    ve tudo pronto e pula. Bloqueante (e nao try-e-sai) garante que nenhum
    worker comece a servir request antes do schema estar pronto.

    Lock 7741 (livre; ver chaves em uso em seru_cron.py e migrations_legacy).
    SQLite (local/teste) nao tem multiplos workers nem `pg_advisory_lock` —
    roda direto.
    """
    is_pg = app.config.get('SQLALCHEMY_DATABASE_URI', '').startswith('postgresql')
    if not is_pg:
        db.create_all()
        _migrate(app)
        _alembic_stamp_se_necessario(app)
        _cutover_baixa_venda(app)
        return

    from sqlalchemy import text
    lock_conn = db.engine.connect()
    try:
        # Bloqueia ate conseguir o lock (outro worker pode estar criando).
        lock_conn.execute(text('SELECT pg_advisory_lock(7741)'))
        db.create_all()
        _migrate(app)
        _alembic_stamp_se_necessario(app)
        _cutover_baixa_venda(app)
    finally:
        try:
            lock_conn.execute(text('SELECT pg_advisory_unlock(7741)'))
        except Exception:
            pass
        lock_conn.close()


def _cutover_baixa_venda(app):
    """Cutover do motor unico de baixa, no startup (serializado pelo lock 7741
    do `_setup_schema`). Idempotente e best-effort:
    1) backfill do VendaMapa a partir de SeruProdutoMap + LojaProdutoMap;
    2) migra as fracoes pendentes (SeruDebito + LojaDebito) pro DebitoEstoque.

    Roda a cada deploy mas vira no-op barato depois do 1o (fontes zeradas).
    Pula em teste (PYTEST_RUNNING). Transicional — sai quando os mapas velhos
    forem removidos na fase de limpeza."""
    if os.environ.get('PYTEST_RUNNING'):
        return
    try:
        from app.services.venda_mapa_migracao import backfill_venda_mapa, migrar_fracoes_para_debito_estoque
        backfill_venda_mapa()
        r = migrar_fracoes_para_debito_estoque()
        if r['itens'] or r['movs_migrados']:
            app.logger.info('cutover baixa: %s', r)
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        app.logger.warning('cutover baixa_venda falhou (segue sem bloquear): %s', e)


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
    # Pula em testes: conftest seta PYTEST_RUNNING. Em pytest com SQLite
    # :memory:, Alembic abre conexao propria e nao ve as tabelas criadas
    # por `db.create_all()`. Como conftest ja faz create_all no estado
    # final, nao precisa do upgrade.
    if os.environ.get('PYTEST_RUNNING'):
        return
    from sqlalchemy import inspect
    try:
        insp = inspect(db.engine)
        tabelas = set(insp.get_table_names())
        if 'usuario' not in tabelas:
            return  # banco novo/vazio
        # Fase 1: stamp BASELINE (nao head!) se Alembic nunca foi inicializado.
        # Stampar 'head' pularia migrations posteriores que devem ser aplicadas
        # no banco prod (ex: B4 muda Float pra Numeric, B5 adiciona FKs).
        if 'alembic_version' not in tabelas:
            from flask_migrate import stamp
            stamp(directory='migrations', revision='69d82afed149')
            logger.warning(
                'Alembic: baseline marcada — schema legado adotado. '
                'Upgrade pra head vai rodar a seguir.'
            )
        # Fase 2: aplica migrations pendentes ate head
        from flask_migrate import upgrade as _upgrade
        _upgrade(directory='migrations')
    except Exception:  # noqa: BLE001
        logger.exception(
            'Alembic stamp/upgrade falhou. Verificar manualmente com '
            '`railway run flask db current` e `flask db upgrade`.'
        )

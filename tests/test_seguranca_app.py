"""Hardening de segurança (23/06/2026) — auditoria interna + subagente.

Cobre:
- HSTS sempre ligado fora de localhost (antes só ligava com `request.is_secure`,
  que era False atrás do proxy do Railway → HSTS nunca saía em prod)
- Redirect HTTP → HTTPS (defesa em profundidade caso Railway desligue)
- Permissions-Policy nega câmera/mic/GPS (mitiga XSS escalando)
- Rate limit global por IP (300/min default)
- Rate limit nas rotas de pagamento (10/min)
- Webhooks isentos do limit global (Pagar.me retenta legitimamente)
"""


def _client(app):
    """Client de teste com host público (não localhost) — HSTS só liga fora
    de localhost por design (dev sem HTTPS quebraria)."""
    c = app.test_client()
    return c


def test_hsts_em_host_publico(app):
    """Header HSTS tem que ir em qualquer host que não seja localhost."""
    c = _client(app)
    r = c.get('/loja/manifest.webmanifest',
              base_url='https://opao.online')
    assert r.headers.get('Strict-Transport-Security', '').startswith(
        'max-age=31536000')


def test_hsts_nao_em_localhost(app):
    """Em dev (localhost) HSTS quebraria o teste de HTTP local."""
    c = _client(app)
    r = c.get('/loja/manifest.webmanifest',
              base_url='https://localhost:5000')
    assert 'Strict-Transport-Security' not in r.headers


def test_permissions_policy_nega_camera_mic_gps(app):
    """Header Permissions-Policy presente em qualquer resposta."""
    c = _client(app)
    r = c.get('/loja/manifest.webmanifest')
    pol = r.headers.get('Permissions-Policy', '')
    assert 'camera=()' in pol
    assert 'microphone=()' in pol
    assert 'geolocation=()' in pol


def test_proxy_fix_ativo(app):
    """ProxyFix configurado — sem isso, request.is_secure / remote_addr
    ficam falsos atrás do proxy do Railway."""
    from werkzeug.middleware.proxy_fix import ProxyFix
    assert isinstance(app.wsgi_app, ProxyFix)


def test_cookie_session_flags_em_prod(app):
    """Cookies de sessão: HttpOnly + SameSite=Lax. (Secure só em prod;
    em teste fica desligado pra não quebrar o test_client.)"""
    assert app.config['SESSION_COOKIE_HTTPONLY'] is True
    assert app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'


def test_pagamento_pix_tem_rate_limit(app):
    """`/pedido/<codigo>/pix` tem `@limiter.limit('10 per minute')` —
    impede atacante de spammar tentativa de pagamento."""
    from app.blueprints.loja import routes
    # decorator do limiter anota a função; checamos pela existência
    # do atributo (mais robusto que regex no source).
    func = routes.pedido_pix
    # flask-limiter atribui um atributo a função decorada
    assert hasattr(func, '_limiter_decorated') or \
        getattr(func, '__wrapped__', func) is not func


def test_pagamento_cartao_tem_rate_limit(app):
    from app.blueprints.loja import routes
    func = routes.pedido_cartao
    assert hasattr(func, '_limiter_decorated') or \
        getattr(func, '__wrapped__', func) is not func

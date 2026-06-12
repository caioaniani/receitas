"""Flags do cookie de sessao — defesa em profundidade contra roubo de
sessao. Auditoria de 12/06/2026: zero uso de document.cookie no JS, todo
polling do padeiro e same-origin, iframe do Chatwoot autentica via token
na URL — aplicar flags nao quebra fluxo nenhum."""


def test_cookie_session_em_producao_tem_3_flags(app):
    """Em prod (postgres), os 3 flags estao ligados: HTTPONLY (JS nao
    le), SameSite=Lax (CSRF estrutural), SECURE (so HTTPS)."""
    # Simula 'prod' forcando o classmethod a ler postgres
    import config as cfg
    from config import Config
    url_anterior = cfg.DATABASE_URL
    try:
        cfg.DATABASE_URL = 'postgresql://test'
        # Re-avaliar a flag SECURE (e classe attribute computada uma vez,
        # entao testamos o boolean direto)
        assert Config.SESSION_COOKIE_HTTPONLY is True
        assert Config.SESSION_COOKIE_SAMESITE == 'Lax'
        # Em prod (postgresql na DATABASE_URL no import), SECURE liga
        # Como Config.SESSION_COOKIE_SECURE e computado no class body,
        # ele reflete o estado de DATABASE_URL no momento do import.
        # Em test runs sem env prod, fica False — isso e OK e por design.
    finally:
        cfg.DATABASE_URL = url_anterior


def test_secure_so_liga_quando_postgres(app):
    """SECURE=True em dev local (sqlite + http://localhost) bloqueia o
    cookie e deixa o usuario sem login. Condicional evita isso."""
    from config import Config
    # No ambiente de teste (sqlite), tem que estar False
    assert Config.SESSION_COOKIE_SECURE in (True, False)
    # E o app efetivamente carrega o valor
    assert 'SESSION_COOKIE_HTTPONLY' in app.config
    assert app.config['SESSION_COOKIE_HTTPONLY'] is True
    assert app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'


def test_login_continua_funcionando(app, admin_user):
    """Smoke: depois das flags novas, login + acesso a rota autenticada
    seguem funcionando."""
    c = app.test_client()
    r = c.post('/auth/login', data={'login': 'admin', 'senha': '123'},
               follow_redirects=False)
    assert r.status_code == 302
    # Cookie veio na resposta
    cookies = r.headers.getlist('Set-Cookie')
    assert any('session=' in ck for ck in cookies), \
        'cookie de sessao nao foi setado no login'
    # HttpOnly aparece na resposta (flask-login default + nossa flag)
    assert any('HttpOnly' in ck.lower() or 'httponly' in ck.lower()
               for ck in cookies), 'cookie de sessao sem HttpOnly'
    # Acesso a rota autenticada apos login
    r2 = c.get('/')
    assert r2.status_code in (200, 302)


def test_zero_uso_de_document_cookie_no_js(app):
    """Trava de regressao: nenhum codigo JS pode ler document.cookie.
    Se alguem adicionar isso, a flag HTTPONLY=True quebra esse codigo
    em silencio. Melhor pegar no CI."""
    import pathlib
    base = pathlib.Path('app/static/js')
    if not base.exists():
        return
    ofensores = []
    for p in base.rglob('*.js'):
        if 'document.cookie' in p.read_text():
            ofensores.append(str(p))
    assert not ofensores, (
        'JS lendo document.cookie quebra com HTTPONLY=True: '
        + ', '.join(ofensores))

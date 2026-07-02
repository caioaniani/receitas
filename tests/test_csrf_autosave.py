"""Recuperação de CSRF expirado/inválido (02/07/2026, em duas rodadas).

Rodada 1 (autosave via fetch): o token embutido na página expirava em 1h
(WTF_CSRF_TIME_LIMIT default do Flask-WTF). Aba deixada aberta (ex:
cronograma da indústria) falhava TODO save via fetch com um 400 HTML — o
front fazia resp.json(), estourava SyntaxError e mostrava alert críptico.
Fix: handler de CSRFError devolve JSON `erro='csrf_expirada'` quando o
request é JSON + GET /auth/csrf-token (logado) devolve token novo pro front
re-tentar.

Rodada 2 (form HTML — caso real /telaindustriateste/enviar): o botão
"Enviar ao padeiro" é um <form> comum; depois de 1h de aba aberta o POST
morria na página "400 The CSRF token has expired" crua. Fix canônico na
raiz: `WTF_CSRF_TIME_LIMIT = None` (config.py) — o token vale a sessão
inteira; a proteção CSRF vem do token ser secreto e amarrado à sessão, não
do TTL. O handler agora cobre só o residual (sessão trocada/cookie apagado)
redirecionando o form de volta à tela de origem com flash + token novo —
NUNCA pra referrer de outra origem (ataque cross-site cai na home).

Nos testes o conftest desliga o CSRF; aqui religamos por teste (config é
snapshot/restaurada pelo conftest).
"""


def test_csrf_time_limit_none_trava(app):
    """Trava de regressão: token CSRF vale a sessão inteira. Se alguém
    remover do config.py, o default de 1h volta e todo form de aba aberta
    quebra de novo."""
    assert app.config['WTF_CSRF_TIME_LIMIT'] is None


def test_csrf_error_devolve_json_para_fetch(app):
    """POST JSON sem token com CSRF ligado → 400 JSON com erro='csrf_expirada'
    (o CSRFProtect roda em before_request, antes até do login_required)."""
    app.config['WTF_CSRF_ENABLED'] = True
    client = app.test_client()
    resp = client.post('/telaindustriateste/celula',
                       json={'receita_id': 1, 'data': '2026-01-01', 'qtd': 1})
    assert resp.status_code == 400
    d = resp.get_json()
    assert d['ok'] is False
    assert d['erro'] == 'csrf_expirada'


def test_csrf_error_form_html_redireciona_com_aviso(app):
    """Form HTML sem token válido volta pra tela de origem (mesma origem)
    com flash de aviso — não mais a página 400 crua."""
    app.config['WTF_CSRF_ENABLED'] = True
    client = app.test_client()
    resp = client.post('/auth/login', data={'login': 'x', 'senha': 'y'},
                       headers={'Referer': 'http://localhost/auth/login'})
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/auth/login')
    # O aviso fica na sessão e aparece na próxima página.
    pagina = client.get('/auth/login').get_data(as_text=True)
    assert 'Sessão de segurança expirada' in pagina


def test_csrf_error_form_referrer_externo_cai_na_home(app):
    """Referrer de OUTRA origem (POST cross-site de verdade) nunca vira
    destino do redirect — cai na home."""
    app.config['WTF_CSRF_ENABLED'] = True
    client = app.test_client()
    resp = client.post('/auth/login', data={'login': 'x', 'senha': 'y'},
                       headers={'Referer': 'https://atacante.example/form'})
    assert resp.status_code == 302
    assert resp.headers['Location'] in ('/', 'http://localhost/')


def test_csrf_token_novo_exige_login(app):
    client = app.test_client()
    resp = client.get('/auth/csrf-token')
    assert resp.status_code in (302, 401)


def test_csrf_token_novo_devolve_token(app, admin_user):
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'})
    resp = client.get('/auth/csrf-token')
    assert resp.status_code == 200
    d = resp.get_json()
    assert d['ok'] is True
    assert d['token']

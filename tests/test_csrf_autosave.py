"""Recuperação de CSRF expirado nos autosaves via fetch (02/07/2026).

O token CSRF embutido na página expira em 1h (WTF_CSRF_TIME_LIMIT default do
Flask-WTF). Aba deixada aberta (ex: cronograma da indústria) falhava TODO save
via fetch com um 400 HTML — o front fazia resp.json(), estourava SyntaxError e
mostrava alert críptico. Agora:
- handler de CSRFError em app/__init__.py devolve JSON `erro='csrf_expirada'`
  quando o request é JSON (fetch); form HTML segue com o 400 padrão;
- GET /auth/csrf-token (logado) devolve token novo pra o front re-tentar.

Nos testes o conftest desliga o CSRF; aqui religamos por teste (config é
snapshot/restaurada pelo conftest).
"""


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


def test_csrf_error_form_html_mantem_400_padrao(app):
    """Form HTML (não-JSON) sem token segue com a página 400 padrão — o
    handler só muda o formato pra requests JSON."""
    app.config['WTF_CSRF_ENABLED'] = True
    client = app.test_client()
    resp = client.post('/auth/login', data={'login': 'x', 'senha': 'y'})
    assert resp.status_code == 400
    assert 'text/html' in resp.content_type


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

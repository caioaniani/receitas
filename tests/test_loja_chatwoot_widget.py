"""Widget do Chatwoot na loja (19/06/2026).

Botão de chat no canto da vitrine. Fail-open: sem `CHATWOOT_WEBSITE_TOKEN`,
o widget não aparece (e o CSP segue fechado). Com a env setada, injeta o
snippet padrão do Chatwoot v4 no `_base.html` e abre o CSP só pro domínio do
Chatwoot configurado (script + websocket + iframe + imagens).
"""


def _staff(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Op', login='op', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def test_widget_desligado_por_padrao(app):
    """Sem CHATWOOT_WEBSITE_TOKEN, o snippet não aparece — fail-open."""
    app.config['CHATWOOT_WEBSITE_TOKEN'] = ''
    c = _staff(app)
    r = c.get('/loja/')
    assert r.status_code == 200
    assert b'chatwootSDK' not in r.data
    assert b'packs/js/sdk.js' not in r.data


def test_widget_ligado_renderiza_snippet(app):
    app.config['CHATWOOT_WEBSITE_TOKEN'] = 'tok_publico_qualquer'
    app.config['CHATWOOT_PUBLIC_URL'] = (
        'https://atendimento.opaopadariaartesanal.com.br')
    c = _staff(app)
    r = c.get('/loja/')
    assert r.status_code == 200
    assert b'chatwootSDK' in r.data
    assert b'tok_publico_qualquer' in r.data
    # BASE_URL é injetado e o sdk.js é carregado de BASE_URL + "/packs/js/sdk.js"
    assert b'atendimento.opaopadariaartesanal.com.br' in r.data
    assert b'/packs/js/sdk.js' in r.data


def test_csp_libera_chatwoot_quando_ligado(app):
    """Com widget ligado, o CSP da loja libera script + websocket + iframe
    do domínio do Chatwoot. Sem ligar, o CSP segue fechado."""
    app.config['CHATWOOT_WEBSITE_TOKEN'] = 'tok'
    app.config['CHATWOOT_PUBLIC_URL'] = (
        'https://atendimento.opaopadariaartesanal.com.br')
    c = _staff(app)
    r = c.get('/loja/')
    csp = r.headers.get('Content-Security-Policy', '')
    assert 'https://atendimento.opaopadariaartesanal.com.br' in csp
    assert 'wss://atendimento.opaopadariaartesanal.com.br' in csp


def test_csp_nao_vaza_chatwoot_quando_desligado(app):
    app.config['CHATWOOT_WEBSITE_TOKEN'] = ''
    app.config['CHATWOOT_PUBLIC_URL'] = (
        'https://atendimento.opaopadariaartesanal.com.br')
    c = _staff(app)
    r = c.get('/loja/')
    csp = r.headers.get('Content-Security-Policy', '')
    # com widget desligado, o CSP da loja NÃO menciona o Chatwoot
    assert 'atendimento.opaopadariaartesanal.com.br' not in csp


def test_widget_nao_aparece_no_admin(app):
    """O snippet vive só em /loja/* — não polui o painel admin."""
    app.config['CHATWOOT_WEBSITE_TOKEN'] = 'tok'
    c = _staff(app)
    r = c.get('/admin/loja-online')
    assert b'chatwootSDK' not in r.data

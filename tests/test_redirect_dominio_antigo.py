"""Cutover: domínio antigo (VNDA) → 302 pro site novo (19/06/2026).

`SITE_REDIRECT_HOSTS` (CSV) lista hosts que só redirecionam pra
`SITE_REDIRECT_DESTINO`. Vazio = inerte (chave liga/desliga sem deploy).
302 de propósito (reversível, sem cache grudado).
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


def test_redirect_dominio_antigo_para_site_novo(app):
    app.config['SITE_REDIRECT_HOSTS'] = (
        'padariaartesanalonline.com.br,www.padariaartesanalonline.com.br')
    app.config['SITE_REDIRECT_DESTINO'] = 'https://opao.online'
    c = app.test_client()
    # qualquer path do domínio antigo cai na raiz do site novo (paths VNDA
    # não existem aqui — evita 404)
    r = c.get('/produto/qualquer-coisa',
              base_url='http://padariaartesanalonline.com.br')
    assert r.status_code == 302
    assert r.headers['Location'] == 'https://opao.online/'
    # www também
    r2 = c.get('/', base_url='http://www.padariaartesanalonline.com.br')
    assert r2.status_code == 302
    assert r2.headers['Location'] == 'https://opao.online/'


def test_redirect_desligado_por_padrao(app):
    app.config['SITE_REDIRECT_HOSTS'] = ''  # default
    c = app.test_client()
    r = c.get('/', base_url='http://padariaartesanalonline.com.br')
    assert r.headers.get('Location') != 'https://opao.online/'


def test_redirect_nao_afeta_gestao(app):
    app.config['SITE_REDIRECT_HOSTS'] = 'padariaartesanalonline.com.br'
    c = app.test_client()
    r = c.get('/health', base_url='http://gestao.opaopadariaartesanal.com.br')
    # host de gestão não é redirecionado pro site
    assert r.headers.get('Location') != 'https://opao.online/'


def test_redirect_nao_afeta_opao_online(app):
    """opao.online (host da loja) NÃO entra no redirect — serve a loja."""
    app.config['SITE_REDIRECT_HOSTS'] = 'padariaartesanalonline.com.br'
    c = app.test_client()
    r = c.get('/', base_url='http://opao.online')
    # opao.online raiz → /loja/ (comportamento da loja), não pro destino externo
    assert r.headers.get('Location', '').endswith('/loja/')


def test_checkout_numero_abre_teclado_numerico(app):
    c = _staff(app)  # gate da loja exige login (LOJA_VISIVEL=0 no teste)
    r = c.get('/loja/checkout')
    assert r.status_code == 200
    # o campo número pede teclado numérico no celular
    assert b'name="numero"' in r.data
    assert b'inputmode="numeric"' in r.data

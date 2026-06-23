"""CSP da loja libera GA4 + Meta Pixel (bug 23/06/2026).

O Pixel mostrava "eventos nunca recebidos" porque o `script-src` da loja não
incluía `connect.facebook.net` — o navegador bloqueava o `fbevents.js` antes
de rodar. Mesma coisa pro GA (`googletagmanager.com`). Estes testes travam que:
- com GA4_ID / META_PIXEL_ID setados, o CSP da loja libera os domínios certos;
- sem as env vars, o CSP NÃO menciona Google/Facebook (fica fechado).
"""


def _staff(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Op', login='op2', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def test_csp_libera_meta_pixel_quando_setado(app):
    app.config['META_PIXEL_ID'] = '1013476578280008'
    c = _staff(app)
    csp = c.get('/loja/').headers.get('Content-Security-Policy', '')
    # script (fbevents.js), img (tr? pixel) e connect (beacon)
    assert 'https://connect.facebook.net' in csp
    assert 'https://www.facebook.com' in csp


def test_csp_libera_ga4_quando_setado(app):
    app.config['GA4_ID'] = 'G-XXXXXXX'
    c = _staff(app)
    csp = c.get('/loja/').headers.get('Content-Security-Policy', '')
    assert 'https://www.googletagmanager.com' in csp
    assert 'https://www.google-analytics.com' in csp


def test_csp_fechado_sem_tracking(app):
    """Sem as env vars, o CSP da loja NÃO libera Google/Facebook."""
    app.config['GA4_ID'] = ''
    app.config['META_PIXEL_ID'] = ''
    c = _staff(app)
    csp = c.get('/loja/').headers.get('Content-Security-Policy', '')
    assert 'facebook' not in csp
    assert 'googletagmanager' not in csp
    assert 'google-analytics' not in csp


def test_csp_pixel_nos_tres_directives(app):
    """O Pixel precisa de script-src (carregar fbevents) + connect-src
    (mandar o evento) + img-src (o tr? de fallback). Sem os três, o evento
    não chega no Meta."""
    app.config['META_PIXEL_ID'] = '1013476578280008'
    c = _staff(app)
    csp = c.get('/loja/').headers.get('Content-Security-Policy', '')
    # quebra o CSP em diretivas pra checar cada uma
    dirs = {}
    for parte in csp.split(';'):
        parte = parte.strip()
        if not parte:
            continue
        nome, _, resto = parte.partition(' ')
        dirs[nome] = resto
    assert 'connect.facebook.net' in dirs.get('script-src', '')
    assert 'connect.facebook.net' in dirs.get('connect-src', '')
    assert 'facebook.com' in dirs.get('img-src', '')

"""PWA da loja (23/06/2026): instalável como app no celular.

O PWA da gestão já existia (`/manifest.webmanifest` + `/sw.js`); aqui é um
PWA SEPARADO pra a loja, com escopo `/loja/` (cliente que instala não cai no
admin). Travas:
- manifest da loja é JSON válido com nome, scope e start_url certos
- SW da loja é servido com Content-Type correto e sem cache
- `_base.html` da loja linka manifest + theme-color e registra o SW
- PWA da gestão (`/manifest.webmanifest`) NÃO é afetado
"""
import json


def test_manifest_da_loja_e_json_e_aponta_pra_loja(app):
    """Manifest separado: nome 'O Pão', scope/start_url '/loja/'."""
    c = app.test_client()
    r = c.get('/loja/manifest.webmanifest')
    assert r.status_code == 200
    assert r.headers['Content-Type'].startswith('application/manifest+json')
    m = json.loads(r.data)
    assert m['name'] == 'O Pão Padaria Artesanal'
    assert m['short_name'] == 'O Pão'
    assert m['scope'] == '/loja/'
    assert m['start_url'] == '/loja/'
    # Cores e ícones obrigatórios pro Chrome aceitar instalação
    assert m['theme_color']
    assert m['background_color']
    assert m['display'] in ('standalone', 'fullscreen', 'minimal-ui')
    icones = m['icons']
    tamanhos = {i['sizes'] for i in icones}
    assert '192x192' in tamanhos
    assert '512x512' in tamanhos


def test_sw_da_loja_servido_corretamente(app):
    """SW precisa de Content-Type JS, sem cache (pra atualizar sozinho)."""
    c = app.test_client()
    r = c.get('/loja/sw.js')
    assert r.status_code == 200
    assert r.headers['Content-Type'].startswith('application/javascript')
    assert r.headers.get('Cache-Control') == 'no-cache'
    assert b'self.addEventListener' in r.data  # é um SW de verdade


def test_base_da_loja_linka_manifest_e_registra_sw(app):
    """O _base.html injeta as tags do PWA. Sem isso, o navegador nem oferece
    'Instalar app'."""
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Op', login='oppwa', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    html = c.get('/loja/').data
    assert b'rel="manifest"' in html
    assert b'theme-color' in html
    assert b'apple-touch-icon' in html
    assert b'apple-mobile-web-app-capable' in html
    assert b'serviceWorker.register' in html


def test_pwa_da_gestao_continua_funcionando(app):
    """Não posso ter quebrado o PWA do admin ao adicionar o da loja."""
    c = app.test_client()
    r = c.get('/manifest.webmanifest')
    assert r.status_code == 200
    m = json.loads(r.data)
    # Padaria O Pão (admin) — diferente do "O Pão Padaria Artesanal" (loja)
    assert m['name'] == 'Padaria O Pão'
    # Scope '/' (gestão) — não conflita com scope '/loja/' do PWA da loja
    assert m['scope'] == '/'


def test_manifest_e_sw_sao_publicos_em_modo_teste(app, monkeypatch):
    """Sem LOJA_VISIVEL, o gate da loja devolve 404 pra anônimo. Manifest e
    SW PRECISAM responder mesmo assim — o navegador os busca antes do
    cliente logar."""
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = app.test_client()
    assert c.get('/loja/manifest.webmanifest').status_code == 200
    assert c.get('/loja/sw.js').status_code == 200


def test_botao_instalar_no_rodape(app):
    """Botão 'Instalar como app' aparece no rodapé com handlers do
    beforeinstallprompt (Android) e modal pra iOS (23/06/2026)."""
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Op', login='oppwabtn', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    html = c.get('/loja/').data
    # botão + bloco do rodapé
    assert b'id="btn-instalar-pwa"' in html
    assert b'id="rodape-instalar"' in html
    # modal de instruções pra iOS
    assert b'id="modal-instalar-ios"' in html
    assert b'Adicionar \xc3\xa0 Tela de In\xc3\xadcio' in html
    # captura prompt nativo do Chrome + esconde quando instala
    assert b'beforeinstallprompt' in html
    assert b'appinstalled' in html
    # display-mode standalone esconde o botão
    assert b'display-mode: standalone' in html

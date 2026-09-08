"""Hub de áreas: cada card da tela inicial abre a página da área
(`/area/<slug>`), que lista as funções daquela área (mesmos links da sidebar,
via macro compartilhado `_area_nav.html`). A permissão da página espelha a do
card (app/nav.py)."""

from collections import Counter
from html.parser import HTMLParser

import pytest


class _AreaLinks(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.primary = []
        self.in_more = False
        self.more_open = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get('class', '').split()
        if tag == 'details' and 'area-v2-more' in classes:
            self.in_more = True
            self.more_open = 'open' in attrs
        if tag == 'a' and {'area-link', 'area-v2-link'}.intersection(classes):
            self.links.append(attrs['href'])
            if not self.in_more:
                self.primary.append(attrs['href'])

    def handle_endtag(self, tag):
        if tag == 'details':
            self.in_more = False


@pytest.mark.parametrize('papel,owner', [
    ('admin', True), ('admin', False), ('gerente', False), ('producao', False),
])
def test_area_simplificada_preserva_destinos_e_permissoes(app, papel, owner):
    """Recolher opções não remove, duplica nem libera funções por papel."""
    uid = _criar(app, 'navegacao', papel, is_owner=owner)
    client = app.test_client()
    _login(client, uid)
    for slug in ('lojas', 'producao', 'catalogo', 'vendas', 'financeiro',
                 'rh', 'relatorios', 'administracao', 'fichas'):
        app.config['UI_V2_ENABLED'] = False
        classic = client.get(f'/area/{slug}')
        app.config['UI_V2_ENABLED'] = True
        simplified = client.get(f'/area/{slug}')
        assert simplified.status_code == classic.status_code
        if simplified.status_code != 200:
            continue
        before = _AreaLinks(classic.get_data(as_text=True))
        after = _AreaLinks(simplified.get_data(as_text=True))
        assert Counter(after.links) == Counter(before.links), slug
        assert 1 <= len(after.primary) <= 4, slug
        assert after.more_open is not True, slug


def test_lojas_destaca_rotina_e_mantem_configuracao_recolhida(app, owner_user):
    client = app.test_client()
    _login(client, owner_user.id)
    links = _AreaLinks(client.get('/area/lojas').get_data(as_text=True))
    assert set(links.primary) == {
        '/checklist/', '/pedidos/', '/pedidos/estoque-loja', '/lista-compras/',
    }
    assert '/checklist/responsaveis' in links.links
    assert links.more_open is False


def _login(client, uid):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True


def _criar(app, login, papel, is_owner=False):
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        u = Usuario(login=login, nome=login, papel=papel, is_owner=is_owner)
        u.set_senha('senha123')
        db.session.add(u)
        db.session.commit()
        return u.id


def test_home_tem_cards_para_area(app, owner_user):
    c = app.test_client()
    _login(c, owner_user.id)
    r = c.get('/')
    assert r.status_code == 200
    # Cards agora apontam pra /area/<slug> (não mais link direto pra 1 rota).
    assert b'/area/lojas' in r.data
    assert b'/area/rh' in r.data
    assert b'/area/administracao' in r.data


def test_area_owner_ve_todas(app, owner_user):
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
    c = app.test_client()
    _login(c, owner_user.id)
    for slug in ('lojas', 'producao', 'catalogo', 'vendas', 'financeiro',
                 'rh', 'relatorios', 'administracao', 'fichas'):
        r = c.get(f'/area/{slug}')
        assert r.status_code == 200, (slug, r.status_code)
        assert b'area-links' in r.data


def test_area_lista_funcoes_da_area(app, owner_user):
    """A página da área traz os links reais daquela área — e só os dela."""
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
    import re
    c = app.test_client()
    _login(c, owner_user.id)
    r = c.get('/area/lojas')
    assert r.status_code == 200
    html = r.data.decode()
    # Recorta só o bloco .area-links (a sidebar, em toda página, lista tudo).
    m = re.search(r'class="area-links">(.*?)</div>\s*</div>', html, re.S)
    assert m, 'bloco .area-links não encontrado'
    bloco = m.group(1)
    assert '/pedidos/estoque-loja' in bloco          # função da área Lojas
    assert 'Desperdício' in bloco
    # E não vaza função de outra área no bloco da área.
    assert '/relatorios/dashboards' not in bloco


def test_area_slug_inexistente_404(app, owner_user):
    c = app.test_client()
    _login(c, owner_user.id)
    assert c.get('/area/naoexiste').status_code == 404


def test_area_rh_bloqueia_admin_nao_owner(app, admin_user):
    """RH é owner-only (igual ao card). Admin comum leva 403; áreas de admin ok."""
    c = app.test_client()
    _login(c, admin_user.id)
    assert c.get('/area/rh').status_code == 403
    assert c.get('/area/financeiro').status_code == 200


def test_area_respeita_permissao_gerente(app):
    """Gerente (não-admin) tem pode_lojas: abre /area/lojas, mas não /area/financeiro."""
    uid = _criar(app, 'gerente', 'gerente')
    c = app.test_client()
    _login(c, uid)
    assert c.get('/area/lojas').status_code == 200
    assert c.get('/area/financeiro').status_code == 403


def test_area_exige_login(app):
    c = app.test_client()
    r = c.get('/area/lojas', follow_redirects=False)
    assert r.status_code in (302, 401)

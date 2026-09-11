"""Ações do estoque ficam agrupadas sem perder destinos ou permissões."""

from collections import Counter
from html.parser import HTMLParser

import pytest
from flask import url_for


class _AcoesEstoque(HTMLParser):
    """Lê somente a navegação local, sem confundir links da sidebar."""

    _VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
             'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.nav_count = 0
        self.controls = []
        self.menus = []
        self.modal_elements = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        in_toolbar = any(
            t == 'nav' and a.get('aria-label') == 'Ações do estoque'
            for t, a in self.stack)
        if tag == 'nav' and attrs.get('aria-label') == 'Ações do estoque':
            self.nav_count += 1
        if in_toolbar:
            if 'dropdown-menu' in attrs.get('class', '').split():
                self.menus.append(attrs)
            if tag in ('a', 'button'):
                menu = next((a for _, a in reversed(self.stack)
                             if 'dropdown-menu' in a.get('class', '').split()),
                            None)
                self.current = {'tag': tag, 'attrs': attrs, 'menu': menu,
                                'text': ''}
                self.controls.append(self.current)
        if (attrs.get('id') == 'modal-ajuste'
                or any(a.get('id') == 'modal-ajuste' for _, a in self.stack)):
            self.modal_elements.append((tag, attrs))
        if tag not in self._VOID:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        if self.current is not None and self.current['tag'] == tag:
            self.current['text'] = ' '.join(self.current['text'].split())
            self.current = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.current is not None:
            self.current['text'] += data


def _client(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


def _pagina(client, loja_id=None):
    response = client.get('/pedidos/estoque-loja',
                          query_string={'loja': loja_id} if loja_id else {})
    assert response.status_code == 200
    return _AcoesEstoque(response.get_data(as_text=True))


def _destinos(app, loja_id):
    with app.test_request_context():
        conferencia = {
            url_for('pedidos.conferencia', loja=loja_id),
            url_for('pedidos.estoque_loja_conferencia_lote', loja=loja_id),
            url_for('pedidos.estoque_loja_balanco_template'),
        }
        mais = {
            url_for('pedidos.estoque_loja_entrada_lote', loja=loja_id),
            url_for('pedidos.estoque_loja_saida_lote', loja=loja_id),
            url_for('pedidos.vendas_manuais', loja_id=loja_id),
            url_for('pedidos.estoque_loja_historico', loja=loja_id),
            url_for('pedidos.estoque_loja_saude'),
            url_for('pedidos.precos_loja', loja_id=loja_id),
            url_for('pedidos.estoque_loja_mapeamentos'),
        }
        sugerir = url_for('pedidos.sugerir_pedido', loja_id=loja_id)
    return conferencia, mais, sugerir


@pytest.mark.parametrize('ui_v2', [False, True])
def test_admin_tem_tres_acoes_principais_e_menus_fechados(
        app, admin_user, loja, ui_v2):
    app.config['UI_V2_ENABLED'] = ui_v2
    pagina = _pagina(_client(app, admin_user), loja.id)

    assert pagina.nav_count == 1
    principais = [c for c in pagina.controls if c['menu'] is None]
    assert [c['text'] for c in principais] == [
        'Conferir estoque', 'Sugerir pedido', 'Mais ações']
    assert [c['tag'] for c in principais] == ['button', 'a', 'button']
    assert len(pagina.menus) == 2
    for menu in pagina.menus:
        assert 'show' not in menu.get('class', '').split()
    for controle in (principais[0], principais[2]):
        assert controle['attrs']['type'] == 'button'
        assert controle['attrs']['data-bs-toggle'] == 'dropdown'
        assert controle['attrs']['aria-expanded'] == 'false'


def test_menus_preservam_todos_os_destinos_e_a_loja_selecionada(
        app, admin_user, loja):
    from app.extensions import db
    from app.models import Loja

    outra = Loja(nome='Segunda loja', ativa=True)
    db.session.add(outra)
    db.session.commit()
    client = _client(app, admin_user)

    # Trocar a seleção deve trocar todos os destinos que dependem de loja.
    for loja_id in (loja.id, outra.id):
        pagina = _pagina(client, loja_id)
        conferencia, mais, sugerir = _destinos(app, loja_id)
        links = [c for c in pagina.controls if c['tag'] == 'a']
        assert Counter(c['attrs']['href'] for c in links) == Counter(
            conferencia | mais | {sugerir})
        grupos = {c['text']: c['attrs']['id'] for c in pagina.controls
                  if c['attrs'].get('data-bs-toggle') == 'dropdown'}
        for link in links:
            href = link['attrs']['href']
            if href == sugerir:
                assert link['menu'] is None
            else:
                grupo = 'Conferir estoque' if href in conferencia else 'Mais ações'
                assert link['menu']['aria-labelledby'] == grupos[grupo]


def test_entrada_ajuste_continua_abrindo_o_modal_da_loja(
        app, admin_user, loja, catalogo):
    pagina = _pagina(_client(app, admin_user), loja.id)
    ajustes = [c for c in pagina.controls
               if c['attrs'].get('data-bs-target') == '#modal-ajuste']
    assert len(ajustes) == 1
    ajuste = ajustes[0]
    assert ajuste['tag'] == 'button'
    assert ajuste['attrs']['type'] == 'button'
    assert ajuste['attrs']['data-bs-toggle'] == 'modal'
    assert ajuste['menu'] is not None
    formularios = [attrs for tag, attrs in pagina.modal_elements if tag == 'form']
    with app.test_request_context():
        assert formularios == [{
            'method': 'POST', 'action': url_for('pedidos.estoque_loja_ajuste')}]
    campos = {attrs['name']: attrs for tag, attrs in pagina.modal_elements
              if tag in ('input', 'select') and 'name' in attrs}
    assert campos['loja_id']['value'] == str(loja.id)
    assert {'csrf_token', 'item_id', 'operacao', 'quantidade', 'motivo'} <= campos.keys()
    opcoes = {attrs.get('value') for tag, attrs in pagina.modal_elements
              if tag == 'option'}
    assert f"p_{catalogo['produto'].id}" in opcoes


def test_admin_sem_loja_exibe_apenas_saude_em_mais_acoes(app, admin_user, loja):
    pagina = _pagina(_client(app, admin_user))
    assert pagina.nav_count == 1
    principais = [c for c in pagina.controls if c['menu'] is None]
    assert [c['text'] for c in principais] == ['Mais ações']
    links = [c for c in pagina.controls if c['tag'] == 'a']
    with app.test_request_context():
        assert [c['attrs']['href'] for c in links] == [
            url_for('pedidos.estoque_loja_saude')]
    assert links[0]['menu'] is not None
    assert pagina.modal_elements == []


def test_gerente_nao_recebe_acoes_administrativas(app, loja):
    from app.extensions import db
    from app.models import Usuario

    gerente = Usuario(nome='Gerente de loja', login='gerente-nav',
                      papel='gerente', loja_id=loja.id)
    gerente.set_senha('senha-teste')
    db.session.add(gerente)
    db.session.commit()
    pagina = _pagina(_client(app, gerente), loja.id)
    assert pagina.controls == []
    assert pagina.modal_elements == []


def test_consultar_navegacao_nao_altera_saldo_ou_movimentos(
        app, admin_user, loja, catalogo):
    from app.extensions import db
    from app.models import EstoqueLoja, MovEstoqueLoja

    estoque = EstoqueLoja(loja_id=loja.id, produto_id=catalogo['produto'].id,
                          quantidade=17, quantidade_reservada=3,
                          estoque_minimo=4, pedido_minimo_diario=2)
    db.session.add(estoque)
    db.session.commit()
    estoque_id = estoque.id
    movimentos_antes = MovEstoqueLoja.query.count()

    _pagina(_client(app, admin_user), loja.id)

    db.session.expire_all()
    atual = db.session.get(EstoqueLoja, estoque_id)
    assert EstoqueLoja.query.count() == 1
    assert (atual.quantidade, atual.quantidade_reservada,
            atual.estoque_minimo, atual.pedido_minimo_diario) == (17, 3, 4, 2)
    assert MovEstoqueLoja.query.count() == movimentos_antes

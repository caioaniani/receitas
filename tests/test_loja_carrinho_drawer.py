"""Drawer do carrinho — bottom sheet que abre automaticamente quando o
cliente adiciona o PRIMEIRO item ao carrinho (decisão do dono 18/06/2026).

Não dá pra rodar JS aqui, então a cobertura é:
- A estrutura HTML do drawer existe em todas as páginas da loja.
- O JS no carrinho.js contém os hooks chave (abre no primeiro item, fecha
  no ESC/overlay, ícone do carrinho dispara abrir).
- Os botões do drawer apontam pra checkout e a página dedicada continua
  acessível (regressão).
"""
import os


def _admin_logado(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Admin', login='adm', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def test_drawer_presente_em_todas_paginas_da_loja(app, monkeypatch):
    """O drawer está no _base.html — então em TODAS as páginas (home,
    produto, checkout, conta...). Verifica home + entrar (não exigem
    catálogo)."""
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    c = app.test_client()
    for path in ('/loja/', '/loja/entrar'):
        r = c.get(path)
        assert r.status_code == 200, path
        assert b'id="cart-drawer"' in r.data, path
        assert b'cart-drawer-panel' in r.data, path
        assert b'cart-drawer-overlay' in r.data, path


def test_drawer_tem_acoes_checkout_e_continuar(app, monkeypatch):
    """O drawer mostra os 2 caminhos: continuar comprando (fecha) +
    ir pro checkout (navega)."""
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    c = app.test_client()
    r = c.get('/loja/')
    assert b'Continuar comprando' in r.data
    assert b'Ir para o checkout' in r.data
    assert b'/loja/checkout' in r.data


def test_drawer_hidden_por_padrao(app, monkeypatch):
    """O drawer começa escondido (`hidden` + `aria-hidden=true`) — JS
    abre quando o cliente adiciona o primeiro item."""
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    c = app.test_client()
    r = c.get('/loja/')
    # `hidden` é atributo booleano: aparece sozinho na tag.
    assert b'aria-hidden="true"' in r.data


def test_drawer_subtotal_inicia_zero(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    c = app.test_client()
    r = c.get('/loja/')
    assert b'id="cart-drawer-total-valor"' in r.data
    assert b'R$ 0,00' in r.data


def test_pagina_dedicada_carrinho_ainda_acessivel(app, monkeypatch):
    """Regressão: o drawer NÃO substitui a página `/loja/carrinho` —
    ela continua sendo a "view completa" do carrinho."""
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    c = app.test_client()
    r = c.get('/loja/carrinho')
    assert r.status_code == 200


def test_js_abre_drawer_no_primeiro_item(app):
    """O carrinho.js dispara `abrirDrawer()` na transição 0 → 1 item."""
    js_path = os.path.join(os.path.dirname(__file__), '..',
                            'app', 'static', 'loja', 'carrinho.js')
    with open(js_path, encoding='utf-8') as fh:
        js = fh.read()
    # Trecho-chave: detecta primeiro item e abre.
    assert 'antesQtd === 0' in js
    assert 'abrirDrawer()' in js


def test_js_tem_handlers_de_fechar(app):
    """ESC, overlay e botão 'continuar comprando' fecham o drawer."""
    js_path = os.path.join(os.path.dirname(__file__), '..',
                            'app', 'static', 'loja', 'carrinho.js')
    with open(js_path, encoding='utf-8') as fh:
        js = fh.read()
    assert "key === 'Escape'" in js
    assert "data-acao=\"fechar\"" in js
    assert 'fecharDrawer()' in js


def test_js_icone_carrinho_abre_drawer(app):
    """Clique no ícone do carrinho no header abre o drawer (em vez de
    navegar pra página dedicada — UX padrão de e-commerce)."""
    js_path = os.path.join(os.path.dirname(__file__), '..',
                            'app', 'static', 'loja', 'carrinho.js')
    with open(js_path, encoding='utf-8') as fh:
        js = fh.read()
    assert ".topo-carrinho" in js
    # E não dispara o abrir quando JÁ estamos na página dedicada (anti-loop)
    assert "'/loja/carrinho'" in js

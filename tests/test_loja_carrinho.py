"""Loja Online — Fase 3: carrinho client-side + casca das rotas.

O carrinho em si vive no navegador (localStorage), fora do alcance do
pytest. O que dá pra travar no servidor: o gate das rotas novas, a casca
HTML que o JS popula, e os data-attributes que a página de produto expõe
pro carrinho.js consumir.
"""


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


def _criar_produto_publicado(db, nome='Box Mimo', preco=166.0,
                              categoria='Cestas'):
    from app.models import Produto
    p = Produto(nome=nome, categoria=categoria, preco_site=preco,
                imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


# ── Gate (herda o before_request do blueprint loja) ──────────────────

def test_carrinho_anonimo_404(app, monkeypatch):
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = app.test_client()
    assert c.get('/loja/carrinho').status_code == 404


def test_checkout_anonimo_404(app, monkeypatch):
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = app.test_client()
    assert c.get('/loja/checkout').status_code == 404


# ── Casca das páginas ────────────────────────────────────────────────

def test_carrinho_staff_renderiza_casca(app, monkeypatch):
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin_logado(app)
    r = c.get('/loja/carrinho')
    assert r.status_code == 200
    # Container que o carrinho.js popula
    assert b'carrinho-app' in r.data
    # carrinho.js carregado no base
    assert b'carrinho.js' in r.data


def test_checkout_renderiza(app, monkeypatch):
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin_logado(app)
    r = c.get('/loja/checkout')
    assert r.status_code == 200
    # Checkout real (Fase 3): formulário presente (fluxo testado a fundo
    # em test_loja_checkout.py).
    assert b'checkout-form' in r.data
    assert b'Finalizar pedido' in r.data


def test_carrinho_nao_colide_com_rota_de_produto(app, monkeypatch):
    """/loja/carrinho é rota estática — Werkzeug prioriza sobre
    /<slug_completo>. Não pode cair no handler de produto (404 de slug)."""
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin_logado(app)
    r = c.get('/loja/carrinho')
    assert r.status_code == 200
    assert b'Seu carrinho' in r.data


# ── Data-attributes da página de produto (contrato com o carrinho.js) ─

def test_produto_expoe_dados_pro_carrinho(app, monkeypatch):
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    from app.extensions import db
    from app.services.loja_catalogo import _slugify
    p = _criar_produto_publicado(db, nome='Box Mimo', preco=166.0)
    c = _admin_logado(app)
    r = c.get(f'/loja/{_slugify(p.nome)}-p{p.id}')
    assert r.status_code == 200
    data = r.data
    assert b'data-add-carrinho' in data
    assert b'data-kind="produto"' in data
    assert f'data-id="{p.id}"'.encode() in data
    assert b'data-nome="Box Mimo"' in data
    assert b'data-preco="166.0"' in data
    # Seletor de quantidade presente
    assert b'id="qtd-add"' in data


def test_header_tem_link_carrinho_com_badge(app, monkeypatch):
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin_logado(app)
    r = c.get('/loja/')
    assert r.status_code == 200
    assert b'cart-badge' in r.data
    assert b'/loja/carrinho' in r.data

"""Loja Online — Fase 2 (16/06/2026): vitrine pública gated.

Cobre o que precisa não regredir:
- gate de acesso (anônimo vê 404; staff logado vê 200; LOJA_VISIVEL=1
  libera pra todos);
- robots.txt SEMPRE acessível (mesmo gated) e diz noindex enquanto teste;
- filtro de "publicado" (preco_site > 0 + ativo);
- página de produto com slug+id (slug errado faz 301).
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


def _criar_receita_publicada(db, nome='Sourdough Tradicional', preco=33.5):
    from app.models import Receita
    r = Receita(nome=nome, preco_site=preco,
                imagem_dropbox_url='https://x/y.jpg',
                rendimento_qtd=1, rendimento_unidade='un',
                peso_base=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def _criar_produto_publicado(db, nome='Box Mimo', preco=166.0,
                               categoria='Cestas'):
    from app.models import Produto
    p = Produto(nome=nome, categoria=categoria, preco_site=preco,
                imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


# ── Gate de acesso ─────────────────────────────────────────────────────

def test_anonimo_ve_404_quando_LOJA_VISIVEL_zero(app, monkeypatch):
    """Decisão do dono: 404 e não 403 — não confessa que a rota existe,
    Google não indexa rota inexistente."""
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = app.test_client()
    assert c.get('/loja/').status_code == 404


def test_staff_logado_ve_200(app, monkeypatch):
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin_logado(app)
    r = c.get('/loja/')
    assert r.status_code == 200
    # Banner "em teste" aparece pra staff (sinaliza que ainda não é pública)
    assert b'em teste' in r.data or b'Em teste' in r.data or b'teste' in r.data


def test_LOJA_VISIVEL_um_libera_pra_anonimo(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    c = app.test_client()
    assert c.get('/loja/').status_code == 200


def test_LOJA_VISIVEL_outros_valores_mantem_gate(app, monkeypatch):
    """Só '1' libera. 'true', 'yes', 'on' etc NÃO — evita que alguém
    libere acidentalmente."""
    c = app.test_client()
    for v in ('true', 'yes', 'on', '2', ''):
        monkeypatch.setenv('LOJA_VISIVEL', v)
        assert c.get('/loja/').status_code == 404, f'{v!r} liberou acidentalmente'


# ── robots.txt: sempre acessível, varia o conteúdo ───────────────────

def test_robots_sempre_acessivel_mesmo_gated(app, monkeypatch):
    """robots.txt PRECISA ser acessível mesmo durante o gate — é
    justamente como dizemos pro Google 'não indexa'."""
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = app.test_client()
    r = c.get('/loja/robots.txt')
    assert r.status_code == 200
    assert b'Disallow: /' in r.data  # NADA indexável enquanto teste


def test_robots_libera_quando_loja_publica(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    c = app.test_client()
    r = c.get('/loja/robots.txt')
    assert b'Allow: /' in r.data
    assert b'Sitemap:' in r.data


# ── Filtro de publicado ──────────────────────────────────────────────

def test_home_lista_so_quem_tem_preco_site_E_ativo(app):
    """preco_site=0 OU NULL → não aparece. arquivada/ativo=False → não aparece."""
    from app.extensions import db
    from app.models import Produto, Receita
    from app.utils import agora
    pub = _criar_receita_publicada(db, nome='Vai aparecer', preco=20.0)
    sem_preco = Receita(nome='Sem preco', preco_site=None,
                         imagem_dropbox_url='https://x/y.jpg',
                         rendimento_qtd=1, rendimento_unidade='un',
                         peso_base=100.0)
    arquivada = Receita(nome='Arquivada', preco_site=15.0,
                         imagem_dropbox_url='https://x/y.jpg',
                         arquivada_em=agora(),
                         rendimento_qtd=1, rendimento_unidade='un',
                         peso_base=100.0)
    inativo = Produto(nome='Inativo', preco_site=30.0,
                       imagem_dropbox_url='https://x/p.jpg', ativo=False)
    db.session.add_all([sem_preco, arquivada, inativo])
    db.session.commit()

    c = _admin_logado(app)
    r = c.get('/loja/')
    assert r.status_code == 200
    assert b'Vai aparecer' in r.data
    assert b'Sem preco' not in r.data
    assert b'Arquivada' not in r.data
    assert b'Inativo' not in r.data
    # Marker do publicado tem que estar lá
    assert pub.nome.encode() in r.data


def test_home_agrupa_por_categoria(app):
    from app.extensions import db
    _criar_receita_publicada(db, nome='Sourdough Trad')
    db.session.commit()
    # Categoria fica no model Receita; o seed acima salvou sem categoria,
    # então cai em "Outros". Garantimos que o título da categoria aparece.
    c = _admin_logado(app)
    r = c.get('/loja/')
    assert r.status_code == 200
    # Mostra como section
    assert b'categoria-titulo' in r.data


# ── Página de produto ────────────────────────────────────────────────

def test_pagina_produto_carrega(app):
    from app.extensions import db
    from app.services.loja_catalogo import _slugify
    p = _criar_produto_publicado(db, nome='Box Mimo')
    c = _admin_logado(app)
    url = f'/loja/{_slugify(p.nome)}-p{p.id}'
    r = c.get(url)
    assert r.status_code == 200
    assert b'Box Mimo' in r.data
    assert b'R$ 166,00' in r.data
    # Fase 3 ligou o carrinho: botão "Adicionar ao carrinho" presente
    assert b'data-add-carrinho' in r.data


def test_slug_errado_redireciona_301(app):
    from app.extensions import db
    p = _criar_produto_publicado(db, nome='Box Mimo')
    c = _admin_logado(app)
    r = c.get(f'/loja/qualquer-coisa-p{p.id}')
    assert r.status_code == 301
    assert 'box-mimo' in r.headers['Location']


def test_id_inexistente_da_404(app):
    c = _admin_logado(app)
    assert c.get('/loja/qualquer-p99999').status_code == 404


def test_item_nao_publicado_da_404(app):
    """Receita arquivada não pode ser acessada nem por URL direta."""
    from app.extensions import db
    from app.models import Receita
    from app.utils import agora
    r = Receita(nome='Sumiu', preco_site=20.0,
                 imagem_dropbox_url='https://x/y.jpg',
                 arquivada_em=agora(),
                 rendimento_qtd=1, rendimento_unidade='un',
                 peso_base=100.0)
    db.session.add(r)
    db.session.commit()
    c = _admin_logado(app)
    assert c.get(f'/loja/sumiu-r{r.id}').status_code == 404


def test_pagina_produto_cesta_lista_composicao(app):
    """Cesta (Produto com itens) mostra 'O que vem na cesta'."""
    from app.extensions import db
    from app.models import ProdutoItem
    p = _criar_produto_publicado(db, nome='Bonjour')
    db.session.add(ProdutoItem(produto_id=p.id, tipo='receita',
                                 item_nome='Sourdough Tradicional',
                                 quantidade=1))
    db.session.add(ProdutoItem(produto_id=p.id, tipo='receita',
                                 item_nome='Croissant Tradicional',
                                 quantidade=2))
    db.session.commit()
    c = _admin_logado(app)
    r = c.get(f'/loja/bonjour-p{p.id}')
    assert r.status_code == 200
    assert b'O que vem' in r.data
    assert b'Sourdough Tradicional' in r.data
    assert b'Croissant Tradicional' in r.data


# ── Service unit tests (lógica isolada) ──────────────────────────────

def test_slugify_basico():
    from app.services.loja_catalogo import _slugify
    assert _slugify('Sourdough Tradicional') == 'sourdough-tradicional'
    assert _slugify('Pão Francês Fermentado') == 'pao-frances-fermentado'
    assert _slugify('Box Mimo 💛') == 'box-mimo'
    assert _slugify('') == 'item'
    assert _slugify(None) == 'item'


def test_parse_slug_id():
    from app.services.loja_catalogo import parse_slug_id
    assert parse_slug_id('sourdough-tradicional-r12') == ('receita', 12, 'sourdough-tradicional')
    assert parse_slug_id('box-mimo-p7') == ('produto', 7, 'box-mimo')
    assert parse_slug_id('sem-sufixo') == (None, None, None)
    assert parse_slug_id('') == (None, None, None)
    # Aceita slug com hífens
    assert parse_slug_id('croissant-amendoas-tradicional-r99') == (
        'receita', 99, 'croissant-amendoas-tradicional')

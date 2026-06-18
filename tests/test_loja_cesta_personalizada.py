"""Cesta personalizada (rodada C — 17/06/2026).

Item com `categoria == 'Cestas Personalizadas'` abre o modo "monte sua
cesta" na página de produto: cliente vê os outros itens do catálogo e
adiciona ao carrinho junto. Cestas/Cestas Personalizadas NÃO entram na
lista pra montar (não dá pra meter cesta dentro de cesta).
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


def _produto_pub(db, **kw):
    from app.models import Produto
    p = Produto(
        nome=kw.get('nome', 'X'),
        categoria=kw.get('categoria', 'Outros'),
        preco_site=kw.get('preco', 10.0),
        imagem_dropbox_url='https://x/y.jpg', ativo=True,
    )
    db.session.add(p)
    db.session.commit()
    return p


def test_eh_personalizada_helper(app):
    """eh_personalizada True só quando categoria bate exatamente
    'Cestas Personalizadas' (case-insensitive)."""
    from app.services import loja_catalogo
    with app.app_context():
        assert loja_catalogo.eh_personalizada(
            {'categoria': 'Cestas Personalizadas'}) is True
        assert loja_catalogo.eh_personalizada(
            {'categoria': 'cestas personalizadas'}) is True
        assert loja_catalogo.eh_personalizada(
            {'categoria': 'Cestas'}) is False
        assert loja_catalogo.eh_personalizada({'categoria': ''}) is False
        assert loja_catalogo.eh_personalizada({}) is False


def test_itens_para_montar_exclui_cestas(app):
    """A lista 'monte sua cesta' NÃO inclui cestas (nem personalizadas, nem
    cestas comuns) — só os outros itens."""
    from app.extensions import db
    from app.services import loja_catalogo
    with app.app_context():
        _produto_pub(db, nome='Pão', categoria='Pães')
        _produto_pub(db, nome='Café', categoria='Bebidas')
        _produto_pub(db, nome='Cesta Mimo', categoria='Cestas')
        _produto_pub(db, nome='Monte a Sua', categoria='Cestas Personalizadas')
        nomes = [i['nome'] for i in loja_catalogo.itens_para_montar()]
    assert 'Pão' in nomes
    assert 'Café' in nomes
    assert 'Cesta Mimo' not in nomes
    assert 'Monte a Sua' not in nomes


def test_itens_para_montar_exclui_o_proprio_item(app):
    """Se passar `excluir_item=X`, X não aparece (evitar item duplicado
    quando o próprio for o de referência)."""
    from app.extensions import db
    from app.services import loja_catalogo
    with app.app_context():
        p = _produto_pub(db, nome='Pão', categoria='Pães')
        nomes = [
            i['nome'] for i in loja_catalogo.itens_para_montar(
                excluir_item={'kind': 'produto', 'id': p.id})
        ]
    assert 'Pão' not in nomes


def test_pagina_personalizada_mostra_monte(app):
    """Página de um produto da categoria 'Cestas Personalizadas' mostra
    a seção 'Monte sua cesta' com outros produtos."""
    from app.extensions import db
    from app.services.loja_catalogo import _slugify
    c = _admin_logado(app)
    with app.app_context():
        cesta = _produto_pub(db, nome='Monte Box',
                             categoria='Cestas Personalizadas', preco=80.0)
        _produto_pub(db, nome='Pão Sourdough', categoria='Pães')
        url = f'/loja/{_slugify(cesta.nome)}-p{cesta.id}'
    r = c.get(url)
    assert r.status_code == 200
    assert b'Monte sua cesta' in r.data
    assert b'P\xc3\xa3o Sourdough' in r.data   # produto p/ montar
    # Card pra montar tem botão de adicionar (data-add)
    assert b'class="card-add"' in r.data


def test_pagina_nao_personalizada_nao_mostra_monte(app):
    """Página de produto comum não mostra 'Monte sua cesta'."""
    from app.extensions import db
    from app.services.loja_catalogo import _slugify
    c = _admin_logado(app)
    with app.app_context():
        p = _produto_pub(db, nome='Pão', categoria='Pães')
        # E pra ter conteúdo na vitrine
        _produto_pub(db, nome='Outro', categoria='Doces')
        url = f'/loja/{_slugify(p.nome)}-p{p.id}'
    r = c.get(url)
    assert r.status_code == 200
    assert b'Monte sua cesta' not in r.data


def test_personalizada_sem_outros_itens_nao_quebra(app):
    """Cesta personalizada SEM outros produtos pra montar não estoura."""
    from app.extensions import db
    from app.services.loja_catalogo import _slugify
    c = _admin_logado(app)
    with app.app_context():
        cesta = _produto_pub(db, nome='Solo',
                             categoria='Cestas Personalizadas', preco=50.0)
        url = f'/loja/{_slugify(cesta.nome)}-p{cesta.id}'
    r = c.get(url)
    assert r.status_code == 200
    # Sem itens → seção inteira é omitida
    assert b'Monte sua cesta' not in r.data

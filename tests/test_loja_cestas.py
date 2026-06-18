"""Categoria das cestas (Fase 3) — regra que libera a cartinha no checkout.

Cesta = Produto com composição. seed_cestas_categoria normaliza a categoria
sem clobrar 'Cestas Personalizadas'. A página de produto expõe data-categoria
pro carrinho.js levar a categoria e o checkout decidir mostrar a cartinha.
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


def _cesta(db, nome, categoria=None):
    """Produto COM composição (= cesta)."""
    from app.models import Produto, ProdutoItem
    p = Produto(nome=nome, categoria=categoria, preco_site=100.0,
                imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(p)
    db.session.flush()
    db.session.add(ProdutoItem(produto_id=p.id, tipo='receita',
                               item_nome='Sourdough', quantidade=1))
    db.session.commit()
    return p


def test_seed_cestas_categoria_corrige_sem_categoria(app):
    from app.extensions import db
    from app.seed import seed_cestas_categoria
    with app.app_context():
        p = _cesta(db, 'Cesta Nova', categoria=None)
        n = seed_cestas_categoria()
        db.session.refresh(p)
        assert p.categoria == 'Cestas'
        assert n >= 1


def test_seed_cestas_nao_clobra_personalizadas(app):
    from app.extensions import db
    from app.seed import seed_cestas_categoria
    with app.app_context():
        p = _cesta(db, 'Personalizada X', categoria='Cestas Personalizadas')
        seed_cestas_categoria()
        db.session.refresh(p)
        assert p.categoria == 'Cestas Personalizadas'  # preservada


def test_seed_cestas_preserva_categoria_manual_do_dono(app):
    """REGRESSÃO (18/06/2026): o dono move uma cesta (com composição) pra
    'Acompanhamentos' na curadoria. O seed do startup NÃO pode reverter pra
    'Cestas' — a curadoria é a fonte de verdade. (Antes, o seed clobrava
    qualquer categoria sem 'cesta' no nome.)"""
    from app.extensions import db
    from app.seed import seed_cestas_categoria
    with app.app_context():
        p = _cesta(db, 'Iogurte Artesanal 600ml', categoria='Acompanhamentos')
        n = seed_cestas_categoria()
        db.session.refresh(p)
        assert p.categoria == 'Acompanhamentos'  # NÃO virou 'Cestas'
        assert n == 0  # nada foi alterado


def test_seed_cestas_ignora_produto_sem_composicao(app):
    from app.extensions import db
    from app.models import Produto
    from app.seed import seed_cestas_categoria
    with app.app_context():
        # Avulso (sem ProdutoItem) não é cesta — categoria intocada.
        avulso = Produto(nome='Granola 500g', categoria='Acompanhamentos',
                         preco_site=49.0, ativo=True,
                         imagem_dropbox_url='https://x/g.jpg')
        db.session.add(avulso)
        db.session.commit()
        seed_cestas_categoria()
        db.session.refresh(avulso)
        assert avulso.categoria == 'Acompanhamentos'


def test_produto_cesta_expoe_categoria_pro_carrinho(app, monkeypatch):
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    from app.extensions import db
    from app.services.loja_catalogo import _slugify
    c = _admin_logado(app)
    p = _cesta(db, 'Box Mimo', categoria='Cestas')
    r = c.get(f'/loja/{_slugify(p.nome)}-p{p.id}')
    assert r.status_code == 200
    assert b'data-categoria="Cestas"' in r.data

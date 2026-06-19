"""Link de 1 clique do carrinho (/loja/carrinho?add=...).

O bot/atendimento manda um link que JÁ enche o carrinho. O servidor resolve
cada item (preço + estoque REAIS do nosso catálogo), descarta esgotado/
inexistente e injeta o resto via JS. Aqui cobrimos a resolução server-side
(autoritativa) — o lado JS (localStorage) não roda no pytest.
"""
from decimal import Decimal

import pytest
from conftest import _make_receita

pytestmark = pytest.mark.loja_host


def _catalogo(db):
    """Loja do site + 1 produto e 1 receita publicados E estocados, + 1
    produto publicado SEM estoque (esgotado)."""
    from app.models import AppConfig, EstoqueLoja, Loja, Produto
    loja = Loja(nome='Anesio', endereco='Anésio Pinto Rosa, 78', ativa=True)
    db.session.add(loja)
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', loja.id)

    box = Produto(nome='Box Mimo', categoria='Cestas', preco_site=Decimal('166'),
                  imagem_dropbox_url='https://x/box.jpg', ativo=True)
    esgotado = Produto(nome='Caixa Especial', categoria='Cestas',
                       preco_site=Decimal('368'), ativo=True)
    croissant = _make_receita('Croissant Tradicional', categoria='Viennoiserie')
    croissant.preco_site = Decimal('22.50')
    db.session.add_all([box, esgotado, croissant])
    db.session.commit()
    # Estoca SÓ o box e o croissant (a Caixa Especial fica esgotada).
    db.session.add(EstoqueLoja(loja_id=loja.id, produto_id=box.id,
                               quantidade=50))
    db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=croissant.id,
                               quantidade=50))
    db.session.commit()
    return {'box': box.id, 'esgotado': esgotado.id, 'croissant': croissant.id}


def test_resolver_prefill_basico(app):
    from app.blueprints.loja.routes import _resolver_prefill_carrinho
    from app.extensions import db
    with app.app_context():
        ids = _catalogo(db)
        itens, esgotados = _resolver_prefill_carrinho(
            f'p{ids["box"]}:2,r{ids["croissant"]}:1')
        assert esgotados == []
        assert len(itens) == 2
        box = next(i for i in itens if i['kind'] == 'produto')
        assert box['id'] == ids['box']
        assert box['qtd'] == 2
        assert box['nome'] == 'Box Mimo'
        assert float(box['preco']) == 166.0   # preço vem do SERVIDOR
        cr = next(i for i in itens if i['kind'] == 'receita')
        assert cr['qtd'] == 1


def test_resolver_prefill_descarta_esgotado(app):
    from app.blueprints.loja.routes import _resolver_prefill_carrinho
    from app.extensions import db
    with app.app_context():
        ids = _catalogo(db)
        itens, esgotados = _resolver_prefill_carrinho(
            f'p{ids["box"]}:1,p{ids["esgotado"]}:1')
        assert [i['id'] for i in itens] == [ids['box']]
        assert esgotados == ['Caixa Especial']


def test_resolver_prefill_ignora_invalido_e_inexistente(app):
    from app.blueprints.loja.routes import _resolver_prefill_carrinho
    from app.extensions import db
    with app.app_context():
        ids = _catalogo(db)
        # 'xyz' inválido, 'p999999' inexistente, 'p<box>' válido
        itens, esgotados = _resolver_prefill_carrinho(
            f'xyz,p999999:3,p{ids["box"]}')
        assert [i['id'] for i in itens] == [ids['box']]
        assert itens[0]['qtd'] == 1   # sem ':qtd' → 1
        assert esgotados == []


def test_resolver_prefill_qtd_clamp_e_dedup(app):
    from app.blueprints.loja.routes import _resolver_prefill_carrinho
    from app.extensions import db
    with app.app_context():
        ids = _catalogo(db)
        # qtd 500 → teto 99; item repetido → dedup (1 linha)
        itens, _ = _resolver_prefill_carrinho(
            f'p{ids["box"]}:500,p{ids["box"]}:3')
        assert len(itens) == 1
        assert itens[0]['qtd'] == 99


def test_carrinho_route_injeta_prefill(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    from app.extensions import db
    with app.app_context():
        ids = _catalogo(db)
    c = app.test_client()
    r = c.get(f'/loja/carrinho?add=p{ids["box"]}:2')
    assert r.status_code == 200
    assert b'id="cart-prefill"' in r.data
    assert b'Box Mimo' in r.data
    assert b'"qtd": 2' in r.data


def test_carrinho_route_avisa_esgotado(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    from app.extensions import db
    with app.app_context():
        ids = _catalogo(db)
    c = app.test_client()
    r = c.get(f'/loja/carrinho?add=p{ids["esgotado"]}:1')
    assert r.status_code == 200
    assert b'esgotado' in r.data.lower()
    # esgotado não entra no prefill (nenhum bloco de prefill renderizado)
    assert b'id="cart-prefill"' not in r.data


def test_carrinho_route_sem_add_funciona(app, monkeypatch):
    """Carrinho normal (sem ?add=) segue servindo a casca, sem prefill."""
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    c = app.test_client()
    r = c.get('/loja/carrinho')
    assert r.status_code == 200
    assert b'id="cart-prefill"' not in r.data

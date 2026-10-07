"""Acerto do cardápio da LOJA pelo cardápio de papel (07/10/2026)."""
from app.extensions import db
from app.models import MateriaPrima, Produto, ProdutoItem, Receita
from app.services import cardapio_loja_2026_10 as svc


def _rec(nome, **kw):
    r = Receita(nome=nome, categoria='X', rendimento_qtd=1,
                rendimento_unidade='unidades', peso_base=1000, **kw)
    db.session.add(r)
    return r


def _base():
    danish = _rec('Danish de queijo branco', preco_loja=25.0)
    croissant = _rec('Croissant Tradicional')
    brioche = _rec('Brioche')
    queijo = MateriaPrima(nome='Queijo prato', unidade='g')
    db.session.add(queijo)
    prods = {n: Produto(nome=n, categoria='Bebidas', ativo=True, preco_loja=pl)
             for n, pl in [('Adicional de Banana', 6.0), ('Granola 50g', None),
                           ('Chocolate do padre Quente', 18.0),
                           ('Chocolate do padre Quente Desnatado', 18.0),
                           ('Chocolate do padre quente Leite vegetal', 18.0),
                           ('Chocolate do Toddy Quente', 18.0),
                           ('Chocolate do Toddy Quente leite vegetal', 18.0),
                           ('Chocolate do Toddy Frio', 18.0)]}
    qqb = Produto(nome='Queijo Quente no Brioche', categoria='Cestas', ativo=True,
                  preco_loja=35.0)
    db.session.add_all(list(prods.values()) + [qqb])
    db.session.flush()
    db.session.add_all([
        ProdutoItem(produto_id=qqb.id, tipo='receita', receita_id=brioche.id,
                    item_nome='Brioche', quantidade=0.25),
        ProdutoItem(produto_id=qqb.id, tipo='mp', materia_prima_id=queijo.id,
                    item_nome='Queijo prato', quantidade=60),
    ])
    db.session.commit()
    return danish, croissant, queijo, prods


def test_aplica_precos_e_cria_itens(app):
    danish, croissant, queijo, prods = _base()
    r = svc.aplicar()
    db.session.commit()
    assert danish.preco_loja == 28.0
    assert prods['Adicional de Banana'].preco_loja == 7.0
    assert prods['Granola 50g'].preco_loja == 7.0
    assert prods['Chocolate do Toddy Quente'].preco_loja == 15.0
    assert prods['Chocolate do padre quente Leite vegetal'].preco_loja == 15.0
    assert prods['Chocolate do Toddy Frio'].preco_loja == 18.0  # frio fica 18
    assert not r['divergentes'] and not r['nao_achados']
    g = Produto.query.filter_by(nome='Guaraná').one()
    assert g.preco_loja == 10.0 and g.categoria == 'Bebidas' and g.preco_site is None
    m = Produto.query.filter_by(nome='Adicional de Manteiga').one()
    assert m.preco_loja == 6.0 and m.categoria == 'Pães na Chapa'
    qq = Produto.query.filter_by(nome='Queijo Quente no Croissant').one()
    assert qq.preco_loja == 35.0 and qq.categoria == 'Cestas'
    comp = {(i.tipo, i.receita_id or i.materia_prima_id, i.quantidade)
            for i in ProdutoItem.query.filter_by(produto_id=qq.id)}
    assert comp == {('receita', croissant.id, 1), ('mp', queijo.id, 60)}


def test_idempotente_e_respeita_edicao_do_dono(app):
    danish, _, _, prods = _base()
    prods['Adicional de Banana'].preco_loja = 8.0  # dono já mexeu
    db.session.commit()
    r = svc.aplicar()
    db.session.commit()
    assert prods['Adicional de Banana'].preco_loja == 8.0
    assert any(d.startswith('Adicional de Banana') for d in r['divergentes'])
    r2 = svc.aplicar()
    db.session.commit()
    assert not r2['criados'] and len(r2['ja_existiam']) == 3
    assert Produto.query.filter_by(nome='Guaraná').count() == 1
    assert danish.preco_loja == 28.0 and 'Danish de queijo branco' in r2['ja_certos']


def test_sem_brioche_unico_nao_cria_queijo_quente(app):
    _rec('Croissant Tradicional')
    db.session.add(Produto(nome='Queijo Quente no Brioche', ativo=True))
    db.session.commit()
    r = svc.aplicar()
    assert r['qq_croissant'].startswith('nao_criado')
    assert Produto.query.filter_by(nome='Queijo Quente no Croissant').count() == 0

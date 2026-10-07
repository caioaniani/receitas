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


def test_base_com_croissant_no_lugar_do_brioche_copia_como_esta(app):
    """Estado real de prod: 'Queijo Quente no Brioche' tem Croissant Tradicional."""
    croissant = _rec('Croissant Tradicional')
    queijo = MateriaPrima(nome='Queijo prato', unidade='g')
    base = Produto(nome='Queijo Quente no Brioche', categoria='Cestas', ativo=True)
    db.session.add_all([queijo, base])
    db.session.flush()
    db.session.add_all([
        ProdutoItem(produto_id=base.id, tipo='receita', receita_id=croissant.id,
                    item_nome='Croissant Tradicional', quantidade=1),
        ProdutoItem(produto_id=base.id, tipo='mp', materia_prima_id=queijo.id,
                    item_nome='Queijo prato', quantidade=60)])
    db.session.commit()
    r = svc.criar_queijo_quente_croissant()
    db.session.commit()
    assert r['criados'] and r['qq_croissant'] == 'componentes=2'
    qq = Produto.query.filter_by(nome='Queijo Quente no Croissant').one()
    assert qq.preco_loja == 35.0
    comp = {(i.receita_id or i.materia_prima_id, i.quantidade)
            for i in ProdutoItem.query.filter_by(produto_id=qq.id)}
    assert comp == {(croissant.id, 1), (queijo.id, 60)}
    assert ProdutoItem.query.filter_by(produto_id=base.id).count() == 2  # base intocada
    assert svc.criar_queijo_quente_croissant()['ja_existiam'] == ['Queijo Quente no Croissant']


def test_unifica_brioche_que_era_croissant(app):
    croissant = _rec('Croissant Tradicional')
    base = Produto(nome='Queijo Quente no Brioche', categoria='Cestas', ativo=True,
                   preco_loja=35.0)
    db.session.add(base)
    db.session.flush()
    db.session.add(ProdutoItem(produto_id=base.id, tipo='receita', receita_id=croissant.id,
                               item_nome='Croissant Tradicional', quantidade=1))
    db.session.commit()
    svc.criar_queijo_quente_croissant()
    db.session.commit()
    r = svc.unificar_queijo_quente_croissant()
    db.session.commit()
    assert not r['motivo'] and r['renomeado'] == f'#{base.id}'
    ativos = Produto.query.filter_by(nome='Queijo Quente no Croissant', ativo=True).all()
    assert [p.id for p in ativos] == [base.id]
    assert Produto.query.filter_by(nome='Queijo Quente no Croissant (duplicado)',
                                   ativo=False).count() == 1
    assert Produto.query.filter_by(nome='Queijo Quente no Brioche').count() == 0


def test_nao_unifica_se_base_tem_brioche(app):
    _rec('Croissant Tradicional')
    brioche = _rec('Brioche')
    base = Produto(nome='Queijo Quente no Brioche', ativo=True)
    db.session.add(base)
    db.session.flush()
    db.session.add(ProdutoItem(produto_id=base.id, tipo='receita', receita_id=brioche.id,
                               item_nome='Brioche', quantidade=0.2))
    db.session.commit()
    r = svc.unificar_queijo_quente_croissant()
    assert r['motivo'].startswith('composicao_inesperada')
    assert db.session.get(Produto, base.id).nome == 'Queijo Quente no Brioche'


def _cranberry(qtd_prato=40, com_branco=False):
    pao = _rec('Pão de Cranberry')
    prato = MateriaPrima(nome='Queijo Prato', unidade='g')
    branco = MateriaPrima(nome='Queijo Branco', unidade='g')
    p = Produto(nome='Queijo quente no cranberry', ativo=True, preco_loja=35.0)
    db.session.add_all([prato, branco, p])
    db.session.flush()
    db.session.add_all([
        ProdutoItem(produto_id=p.id, tipo='receita', receita_id=pao.id,
                    item_nome='Pão de Cranberry', quantidade=0.2),
        ProdutoItem(produto_id=p.id, tipo='mp', materia_prima_id=prato.id,
                    item_nome='Queijo Prato', quantidade=qtd_prato)])
    if com_branco:
        db.session.add(ProdutoItem(produto_id=p.id, tipo='mp', materia_prima_id=branco.id,
                                   item_nome='Queijo Branco', quantidade=60))
    db.session.commit()
    return p, pao, prato, branco


def test_cranberry_ganha_60g_de_prato_e_de_branco(app):
    p, pao, prato, branco = _cranberry()
    r = svc.acertar_recheio_cranberry()
    db.session.commit()
    assert not r['motivo']
    comp = {(i.receita_id or i.materia_prima_id, i.quantidade)
            for i in ProdutoItem.query.filter_by(produto_id=p.id)}
    assert comp == {(pao.id, 0.2), (prato.id, 60), (branco.id, 60)}


def test_cranberry_ja_editado_nao_mexe(app):
    p, _, _, _ = _cranberry(qtd_prato=60, com_branco=True)
    r = svc.acertar_recheio_cranberry()
    assert r['motivo'].startswith('composicao_inesperada')
    assert ProdutoItem.query.filter_by(produto_id=p.id).count() == 3

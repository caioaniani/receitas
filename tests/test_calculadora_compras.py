"""Calculadora de compras (03/07/2026): item + quantidade → quanto comprar
de matéria-prima. Reusa o motor canônico (`producao.consolidar_lista_compras`
→ `ordem_compra_consolidada`) — os testes travam a CONVERSÃO (unidades →
multiplicador) e a explosão de produto/cesta, não a conta do motor (já
coberta em test_ordem_compra*).
"""
from app.extensions import db
from app.models import (
    MateriaPrima,
    Produto,
    ProdutoItem,
    Receita,
    ReceitaIngrediente,
)


def _mp(nome='Farinha', custo=10.0, estoque=500.0):
    mp = MateriaPrima(nome=nome, unidade='g', custo_por_kg=custo,
                      estoque_atual=estoque, fornecedor='Moinho X')
    db.session.add(mp)
    db.session.commit()
    return mp


def _receita_simples(nome='Pao Calc', peso_base=1000.0, peso_unitario=100.0,
                     mp_nome='Farinha', pct=100.0):
    """Ficha 1 MP a pct% — rendimento_massa_crua = peso_base/peso_unitario."""
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=10,
                rendimento_unidade='un', peso_base=peso_base,
                peso_unitario=peso_unitario)
    db.session.add(r)
    db.session.flush()
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                      ingrediente_nome=mp_nome,
                                      porcentagem=pct))
    db.session.commit()
    return r


def test_receita_converte_unidades_em_mp_e_desconta_estoque(app):
    """20 un de um pão de 100 g (batida de 1 kg, 100% farinha) = 2 batidas =
    2000 g de farinha; com 500 g em estoque → comprar 1500 g (R$ 15,00)."""
    from app.services import calculadora_compras
    with app.app_context():
        _mp('Farinha', custo=10.0, estoque=500.0)
        r = _receita_simples()
        res = calculadora_compras.calcular(
            [{'tipo': 'receita', 'id': r.id, 'qtd': 20}])

    forns = res['compra']['fornecedores']
    assert len(forns) == 1 and forns[0]['nome'] == 'Moinho X'
    item = forns[0]['itens'][0]
    assert item['nome'] == 'Farinha'
    assert round(item['quantidade']) == 2000       # necessário
    assert round(item['comprar']) == 1500          # desconta o estoque
    assert round(item['custo_compra'], 2) == 15.0  # 1,5 kg × R$ 10
    assert res['compras_diretas'] == []


def test_produto_cesta_explode_receita_e_lista_pronto(app):
    """3 cestas com (2× receita + 1× produto pronto): a receita entra na
    explosão de MP (6 un) e o produto pronto vira compra direta (3 un)."""
    from app.services import calculadora_compras
    with app.app_context():
        _mp('Farinha', custo=10.0, estoque=0.0)
        r = _receita_simples()                      # 10 un/batida de 1 kg
        pronto = Produto(nome='Iogurte Pronto', categoria='Laticinios',
                         preco_site=12, ativo=True)
        cesta = Produto(nome='Cesta Cafe', categoria='Cestas',
                        preco_site=100, ativo=True)
        db.session.add_all([pronto, cesta])
        db.session.flush()
        db.session.add_all([
            ProdutoItem(produto_id=cesta.id, tipo='receita', receita_id=r.id,
                        item_nome=r.nome, quantidade=2),
            ProdutoItem(produto_id=cesta.id, tipo='produto',
                        produto_componente_id=pronto.id,
                        item_nome=pronto.nome, quantidade=1),
        ])
        db.session.commit()
        res = calculadora_compras.calcular(
            [{'tipo': 'produto', 'id': cesta.id, 'qtd': 3}])

    item = res['compra']['fornecedores'][0]['itens'][0]
    assert round(item['quantidade']) == 600         # 6 un × 100 g de massa
    assert [c['nome'] for c in res['compras_diretas']] == ['Iogurte Pronto']
    assert res['compras_diretas'][0]['qtd'] == 3


def test_produto_simples_vira_compra_direta(app):
    from app.services import calculadora_compras
    with app.app_context():
        p = Produto(nome='Geleia Calc', categoria='Conservas', preco_site=18,
                    ativo=True)
        db.session.add(p)
        db.session.commit()
        res = calculadora_compras.calcular(
            [{'tipo': 'produto', 'id': p.id, 'qtd': 4}])
    assert res['compra']['fornecedores'] == []
    assert res['compras_diretas'] == [
        {'nome': 'Geleia Calc', 'qtd': 4, 'tipo': 'produto'}]


def test_sub_receita_nao_explode_e_avisa(app):
    """Receita montada (consome sub-receita pronta): a sub NÃO vira compra de
    MP — aparece na seção informativa (mesma semântica do produzir)."""
    from app.services import calculadora_compras
    with app.app_context():
        _mp('Amendoas', custo=80.0, estoque=0.0)
        base = _receita_simples('Croissant Trad Calc')
        almond = Receita(nome='Almond Calc', categoria='Croissants',
                         rendimento_qtd=10, rendimento_unidade='un',
                         peso_base=1000.0, peso_unitario=120.0)
        db.session.add(almond)
        db.session.flush()
        db.session.add_all([
            ReceitaIngrediente(receita_id=almond.id, tipo='receita',
                               ingrediente_nome=base.nome, porcentagem=1,
                               sub_receita_id=base.id),
            ReceitaIngrediente(receita_id=almond.id, tipo='mp',
                               ingrediente_nome='Amendoas', porcentagem=10),
        ])
        db.session.commit()
        res = calculadora_compras.calcular(
            [{'tipo': 'receita', 'id': almond.id, 'qtd': 10}])

    nomes_mp = [i['nome'] for f in res['compra']['fornecedores']
                for i in f['itens']]
    assert 'Amendoas' in nomes_mp                   # MP própria explode
    assert 'Farinha' not in nomes_mp                # a da sub NÃO (usa pronta)
    assert res['sub_receitas'] == [
        {'nome': 'Croissant Trad Calc', 'unidades_base': 10}]


def test_rota_renderiza_e_calcula(app, admin_user):
    with app.app_context():
        _mp('Farinha', custo=10.0, estoque=0.0)
        r = _receita_simples()
        rid = r.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    # GET: form com o select
    g = c.get('/lista-compras/calculadora')
    assert g.status_code == 200
    assert b'Calculadora de compras' in g.data
    # POST: calcula e mostra A COMPRAR
    p = c.post('/lista-compras/calculadora', data={
        'item[]': [f'r_{rid}'], 'qtd[]': ['20'],
    })
    assert p.status_code == 200
    body = p.get_data(as_text=True)
    assert 'A COMPRAR' in body
    assert 'Farinha' in body
    assert 'Moinho X' in body


def test_rota_exige_admin(app):
    from app.models import Usuario
    with app.app_context():
        u = Usuario(nome='Func', login='func-calc', papel='funcionario')
        u.set_senha('x' * 8)
        db.session.add(u)
        db.session.commit()
        uid = u.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    assert c.get('/lista-compras/calculadora').status_code == 403

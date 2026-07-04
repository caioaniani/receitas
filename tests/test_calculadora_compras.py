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


def test_sub_receita_normal_explode_em_mp(app):
    """Sub-receita NORMAL (ex: Massa para folhar) EXPLODE em MP — a manteiga
    do folhado tem que aparecer na compra (caso real 03/07: 300 croissants
    mostravam 105 g de manteiga-folhar porque a massa ficava 'pronta')."""
    from app.services import calculadora_compras
    with app.app_context():
        _mp('Manteiga Folhar', custo=60.0, estoque=0.0)
        # Massa: batida 1 kg, 100% manteiga-folhar, bola de 500 g → 2 bolas/batida
        massa = _receita_simples('Massa Folhar Calc', peso_base=1000.0,
                                 peso_unitario=500.0,
                                 mp_nome='Manteiga Folhar', pct=100.0)
        massa.rendimento_qtd = 2      # DECLARADO: batida rende 2 bolas
        db.session.commit()
        croiss = Receita(nome='Croissant Calc', categoria='Croissants',
                         rendimento_qtd=10, rendimento_unidade='un',
                         peso_base=1000.0, peso_unitario=100.0)
        db.session.add(croiss)
        db.session.flush()
        # 2 bolas de massa POR BATIDA do croissant (batida = 10 un)
        db.session.add(ReceitaIngrediente(receita_id=croiss.id, tipo='receita',
                                          ingrediente_nome=massa.nome,
                                          porcentagem=2,
                                          sub_receita_id=massa.id))
        db.session.commit()
        res = calculadora_compras.calcular(
            [{'tipo': 'receita', 'id': croiss.id, 'qtd': 20}])

    # 20 croissants = 2 batidas → 4 bolas → 2 batidas de massa → 2 kg manteiga
    item = res['compra']['fornecedores'][0]['itens'][0]
    assert item['nome'] == 'Manteiga Folhar'
    assert round(item['quantidade']) == 2000
    assert res['sub_receitas'] == []               # explodiu, não ficou de fora


def test_sub_receita_de_retorno_nao_vira_compra(app):
    """Receita de RETORNO (destino de sobra, ex: Almond consome o retorno do
    croissant): NÃO explode em MP — vem de sobra, não de compra."""
    from app.services import calculadora_compras
    with app.app_context():
        _mp('Amendoas', custo=80.0, estoque=0.0)
        _mp('Farinha', custo=10.0, estoque=0.0)
        # Como em produção: RETORNO tem ficha VAZIA; a ficha (Farinha) mora
        # na receita de ORIGEM (fresca), que aponta retorno_receita_id.
        base = Receita(nome='Croissant Trad Calc', categoria='Croissants',
                       rendimento_qtd=10, rendimento_unidade='un',
                       peso_base=1000.0)                 # retorno, ficha vazia
        db.session.add(base)
        db.session.flush()
        origem = _receita_simples('Croissant Fresco Calc')  # ficha com Farinha
        origem.retorno_receita_id = base.id
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
        res_off = calculadora_compras.calcular(
            [{'tipo': 'receita', 'id': almond.id, 'qtd': 10}],
            explodir_retorno=False)
        res_on = calculadora_compras.calcular(
            [{'tipo': 'receita', 'id': almond.id, 'qtd': 10}],
            explodir_retorno=True)

    # Desligado (usar sobras): retorno fora da compra
    nomes_off = [i['nome'] for f in res_off['compra']['fornecedores']
                 for i in f['itens']]
    assert 'Amendoas' in nomes_off and 'Farinha' not in nomes_off
    # Ligado (padrão, pedido do dono 04/07): explode pela receita de ORIGEM
    # (fresca) — a Farinha dela entra na compra; a linha informativa fica.
    nomes_on = [i['nome'] for f in res_on['compra']['fornecedores']
                for i in f['itens']]
    assert 'Amendoas' in nomes_on and 'Farinha' in nomes_on
    for res in (res_off, res_on):
        assert res['sub_receitas'] == [
            {'nome': 'Croissant Trad Calc', 'unidades_base': 10}]


def test_toggle_sem_estoque_compra_cheia(app):
    """considerar_estoque=False: 'a comprar' = necessário CHEIO, ignorando o
    estoque de MP."""
    from app.services import calculadora_compras
    with app.app_context():
        _mp('Farinha', custo=10.0, estoque=500.0)
        r = _receita_simples()
        res = calculadora_compras.calcular(
            [{'tipo': 'receita', 'id': r.id, 'qtd': 20}],
            considerar_estoque=False)
    item = res['compra']['fornecedores'][0]['itens'][0]
    assert round(item['comprar']) == 2000           # cheio (não 1500)
    assert round(item['custo_compra'], 2) == 20.0   # 2 kg × R$ 10
    assert round(res['compra']['total_compra'], 2) == 20.0


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


def test_mp_un_conta_unidades_nao_porcentagem(app):
    """mp_un (caso real 04/07: Baton/Manteiga p/ Folhar na ficha em UN):
    porcentagem = UNIDADES por batida e custo_por_kg = custo POR UNIDADE
    (custos.py:292). Antes caía no ramo de % → 1 un/batida virava 10 g."""
    from app.services import calculadora_compras
    with app.app_context():
        baton = MateriaPrima(nome='Baton Calc', unidade='un',
                             custo_por_kg=1.40, estoque_atual=5,
                             fornecedor='Choc SA')
        db.session.add(baton)
        db.session.commit()
        # Ficha SÓ com mp_un (sem massa em g) → rendimento cai no CADASTRADO
        # (rendimento_qtd): 10 un por batida.
        r = Receita(nome='Pain Calc', categoria='Viennoiserie',
                    rendimento_qtd=10, rendimento_unidade='un',
                    peso_base=1000.0)
        db.session.add(r)
        db.session.flush()
        # 3 bâtons POR BATIDA (batida = 10 un: 1000 g / 100 g)
        db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp_un',
                                          ingrediente_nome='Baton Calc',
                                          porcentagem=3))
        db.session.commit()
        res = calculadora_compras.calcular(
            [{'tipo': 'receita', 'id': r.id, 'qtd': 100}])

    item = res['compra']['fornecedores'][0]['itens'][0]
    assert item['em_unidades'] is True
    assert round(item['quantidade']) == 30          # 10 batidas × 3 un
    assert round(item['comprar']) == 25             # 30 − 5 em estoque
    assert round(item['custo_compra'], 2) == 35.0   # 25 un × R$ 1,40/un


def test_mp_un_fracionaria_compra_arredonda_pra_cima(app):
    """Caso Pote de Mel (04/07): rendimento fracionário gera 145,63 potes —
    a COMPRA de MP unitária arredonda pra CIMA (146), com custo dos inteiros."""
    from app.services import calculadora_compras
    with app.app_context():
        pote = MateriaPrima(nome='Pote Mel Calc', unidade='un',
                            custo_por_kg=3.0, estoque_atual=0,
                            fornecedor='Apiario')
        db.session.add(pote)
        db.session.commit()
        # rendimento cadastrado 1.03 un/batida → 150 un = 145,63 batidas
        r = Receita(nome='Mel Calc', categoria='Conservas',
                    rendimento_qtd=1.03, rendimento_unidade='un',
                    peso_base=1000.0)
        db.session.add(r)
        db.session.flush()
        db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp_un',
                                          ingrediente_nome='Pote Mel Calc',
                                          porcentagem=1))
        db.session.commit()
        res = calculadora_compras.calcular(
            [{'tipo': 'receita', 'id': r.id, 'qtd': 150}])

    item = res['compra']['fornecedores'][0]['itens'][0]
    assert 145.0 < item['quantidade'] < 146.0       # necessário fracionário
    assert item['comprar'] == 146                   # compra inteira (ceil)
    assert round(item['custo_compra'], 2) == 438.0  # 146 × R$ 3


def test_produto_composto_dentro_da_cesta_explode(app):
    """Iogurte 600ml/Granola (04/07): componente-PRODUTO que TEM composição é
    MONTADO pela padaria → explode (receita vira MP; pote vira compra direta).
    Produto sem composição (Mini Manteigas) segue comprado pronto."""
    from app.services import calculadora_compras
    with app.app_context():
        _mp('Leite', custo=8.0, estoque=0.0)
        rec_iog = _receita_simples('Iogurte Base Calc', mp_nome='Leite')
        pote = MateriaPrima(nome='Pote 600 Calc', unidade='un',
                            custo_por_kg=2.0, estoque_atual=0)
        db.session.add(pote)
        iog600 = Produto(nome='Iogurte 600 Calc', categoria='Laticinios',
                         preco_site=25, ativo=True)
        manteiga = Produto(nome='Mini Manteiga Calc', categoria='Laticinios',
                           preco_site=5, ativo=True)
        cesta = Produto(nome='Cesta Pais Calc', categoria='Cestas',
                        preco_site=200, ativo=True)
        db.session.add_all([iog600, manteiga, cesta])
        db.session.flush()
        db.session.add_all([
            # iogurte600 = 1 receita de iogurte + 1 pote (MP)
            ProdutoItem(produto_id=iog600.id, tipo='receita',
                        receita_id=rec_iog.id, item_nome=rec_iog.nome,
                        quantidade=1),
            ProdutoItem(produto_id=iog600.id, tipo='mp',
                        materia_prima_id=pote.id, item_nome=pote.nome,
                        quantidade=1),
            # cesta = 1 iogurte600 + 1 mini manteiga
            ProdutoItem(produto_id=cesta.id, tipo='produto',
                        produto_componente_id=iog600.id,
                        item_nome=iog600.nome, quantidade=1),
            ProdutoItem(produto_id=cesta.id, tipo='produto',
                        produto_componente_id=manteiga.id,
                        item_nome=manteiga.nome, quantidade=1),
        ])
        db.session.commit()
        res = calculadora_compras.calcular(
            [{'tipo': 'produto', 'id': cesta.id, 'qtd': 10}])

    nomes_mp = [i['nome'] for f in res['compra']['fornecedores']
                for i in f['itens']]
    assert 'Leite' in nomes_mp                       # receita do iogurte explodiu
    diretas = {c['nome']: c['qtd'] for c in res['compras_diretas']}
    assert diretas.get('Mini Manteiga Calc') == 10   # sem composição = pronto
    assert diretas.get('Pote 600 Calc') == 10        # embalagem comprada
    assert 'Iogurte 600 Calc' not in diretas         # foi explodido

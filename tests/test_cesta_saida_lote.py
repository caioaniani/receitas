"""Teste de desempacotamento de cesta em saida em lote (estoque loja).

Bug descoberto na conferencia da Anesio: quando a loja vendia uma cesta
('Family Box'), o sistema tentava subtrair do EstoqueLoja(produto_id=cesta),
mas a loja so tem os componentes em estoque, nao a cesta. Resultado:
nada era descontado, e o estoque acumulava em relacao ao fisico.

Fix: aplicar_saida_lote agora desempacota cesta em componentes e baixa
cada componente individualmente.
"""


def test_saida_lote_desempacota_cesta(app, admin_user, loja, catalogo):
    """Cesta com 5 pao + 3 croissant → vender 2 cestas baixa 10 pao + 6 croissant."""
    from app.extensions import db
    from app.models import (Receita, Produto, ProdutoItem,
                              EstoqueLoja, LojaProdutoMap)
    from app.services.estoque_loja_lote import aplicar_saida_lote

    # Cria 1 nova receita (alem do catalogo) pra ser componente
    pao = Receita(nome='Pao Tradicional Cesta', categoria='Paes',
                   rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0)
    db.session.add(pao)
    db.session.flush()

    # Cria produto cesta com 2 componentes (5x pao + 3x croissant)
    cesta = Produto(nome='Family Box Test', ativo=True)
    db.session.add(cesta)
    db.session.flush()
    db.session.add_all([
        ProdutoItem(produto_id=cesta.id, tipo='receita',
                     item_nome=pao.nome, quantidade=5),
        ProdutoItem(produto_id=cesta.id, tipo='receita',
                     item_nome=catalogo['receita'].nome, quantidade=3),
    ])

    # Estoque inicial: 20 paes, 10 croissants
    db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=pao.id, quantidade=20))
    db.session.add(EstoqueLoja(loja_id=loja.id,
                                  receita_id=catalogo['receita'].id, quantidade=10))

    # Mapping: "Family Box" → cesta (confirmado)
    from datetime import datetime
    mp = LojaProdutoMap(
        nome_digitado='Family Box Test',
        produto_id=cesta.id,
        confirmado_em=datetime.now(),
        confirmado_por=admin_user.id,
        fator_quantidade=1.0,
    )
    db.session.add(mp)
    db.session.commit()

    # Aplica saida: 2 cestas vendidas
    itens = [{
        'linha': 'Family Box Test: 2',
        'nome': 'Family Box Test',
        'quantidade': 2,
        'map_entry': mp,
    }]
    resultado = aplicar_saida_lote(itens, loja.id, admin_user, referencia='Teste')

    # Verificacoes
    assert len(resultado['aplicados']) == 1, \
        f'esperava 1 aplicado, veio {resultado}'

    # Pao: 20 - (2 cestas × 5 paes) = 10
    ep_pao = EstoqueLoja.query.filter_by(loja_id=loja.id, receita_id=pao.id).first()
    assert ep_pao.quantidade == 10, f'pao esperado 10, ficou {ep_pao.quantidade}'

    # Croissant: 10 - (2 × 3) = 4
    ep_croi = EstoqueLoja.query.filter_by(
        loja_id=loja.id, receita_id=catalogo['receita'].id).first()
    assert ep_croi.quantidade == 4, f'croissant esperado 4, ficou {ep_croi.quantidade}'

    # Cesta NAO foi criada em EstoqueLoja
    ep_cesta = EstoqueLoja.query.filter_by(loja_id=loja.id, produto_id=cesta.id).first()
    assert ep_cesta is None, 'nao devia criar EstoqueLoja pra cesta'


def test_saida_lote_produto_normal_continua_funcionando(app, admin_user, loja, catalogo):
    """Produto sem componentes (nao-cesta) continua descontando normalmente."""
    from app.extensions import db
    from app.models import EstoqueLoja, LojaProdutoMap
    from app.services.estoque_loja_lote import aplicar_saida_lote
    from datetime import datetime

    # Produto sem itens (nao eh cesta) — usa o produto do catalogo
    produto = catalogo['produto']
    db.session.add(EstoqueLoja(loja_id=loja.id, produto_id=produto.id, quantidade=10))
    mp = LojaProdutoMap(
        nome_digitado='Pao Frances',
        produto_id=produto.id,
        confirmado_em=datetime.now(),
        confirmado_por=admin_user.id,
        fator_quantidade=1.0,
    )
    db.session.add(mp)
    db.session.commit()

    itens = [{'linha': 'Pao: 3', 'nome': 'Pao Frances', 'quantidade': 3, 'map_entry': mp}]
    resultado = aplicar_saida_lote(itens, loja.id, admin_user, referencia='Teste')

    assert len(resultado['aplicados']) == 1
    ep = EstoqueLoja.query.filter_by(loja_id=loja.id, produto_id=produto.id).first()
    assert ep.quantidade == 7, f'esperado 7, veio {ep.quantidade}'

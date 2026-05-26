"""Smoke tests do fluxo B2B.

Cobre: criar venda baixa estoque, falta saldo registra venda_b2b_sem_estoque,
cancelar venda estorna estoque, receber parcela atualiza valor_pago.
"""
from datetime import date, timedelta


def test_form_nova_venda_tem_typeahead(app, admin_user, catalogo):
    """Formulario de nova venda B2B renderiza o item como typeahead (input de
    busca client-side), nao mais um <select> gigante."""
    cliente = app.test_client()
    cliente.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    r = cliente.get('/b2b/vendas/nova')
    assert r.status_code == 200
    assert b'ta-input' in r.data          # campo de busca (typeahead)
    assert b'function norm' in r.data     # normalizacao sem acento
    assert b'Croissant Tradicional' in r.data  # catalogo disponivel pro filtro JS


def test_form_nova_venda_tem_estado_e_entrega(app, admin_user, catalogo):
    """Formulario B2B coleta data de entrega (fila do padeiro) e estado por item."""
    cliente = app.test_client()
    cliente.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    r = cliente.get('/b2b/vendas/nova')
    assert r.status_code == 200
    assert b'name="data_entrega"' in r.data
    assert b'item_estado[]' in r.data


def test_criar_venda_baixa_estoque(app, admin_user, catalogo):
    """Venda B2B com 1 item baixa do EstoqueProducao corretamente."""
    from app.extensions import db
    from app.models import EstoqueProducao, MovEstoqueProducao
    from app.services import vendas_b2b as svc

    ep = EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=20)
    db.session.add(ep)
    db.session.commit()

    venda = svc.criar_venda(
        cliente_nome='Restaurante Avulso',
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 5, 'preco_unitario': 8.0}],
        user=admin_user,
    )
    assert venda.id is not None
    assert venda.valor_total == 40.0
    db.session.refresh(ep)
    assert ep.quantidade == 15
    movs = MovEstoqueProducao.query.filter_by(estoque_producao_id=ep.id).all()
    assert len(movs) == 1
    assert movs[0].tipo == 'venda_b2b'
    assert movs[0].quantidade == 5


def test_criar_venda_sem_estoque_registra_falta(app, admin_user, catalogo):
    """Quando saldo < qtd, baixa o que tem + registra venda_b2b_sem_estoque."""
    from app.extensions import db
    from app.models import EstoqueProducao, MovEstoqueProducao
    from app.services import vendas_b2b as svc

    ep = EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=3)
    db.session.add(ep)
    db.session.commit()

    venda = svc.criar_venda(
        cliente_nome='X',
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 10, 'preco_unitario': 5.0}],
        user=admin_user,
    )
    db.session.refresh(ep)
    assert ep.quantidade == 0
    movs = sorted(MovEstoqueProducao.query.filter_by(estoque_producao_id=ep.id).all(),
                  key=lambda m: m.tipo)
    assert {m.tipo for m in movs} == {'venda_b2b', 'venda_b2b_sem_estoque'}
    qtds = {m.tipo: m.quantidade for m in movs}
    assert qtds['venda_b2b'] == 3
    assert qtds['venda_b2b_sem_estoque'] == 7


def test_cancelar_venda_estorna_estoque(app, admin_user, catalogo):
    """Cancelar venda devolve qtd vendida ao EstoqueProducao."""
    from app.extensions import db
    from app.models import EstoqueProducao
    from app.services import vendas_b2b as svc

    ep = EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=10)
    db.session.add(ep)
    db.session.commit()
    venda = svc.criar_venda(
        cliente_nome='X',
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 4, 'preco_unitario': 10.0}],
        user=admin_user,
    )
    db.session.refresh(ep)
    assert ep.quantidade == 6
    svc.cancelar_venda(venda, user=admin_user)
    db.session.refresh(ep)
    assert ep.quantidade == 10
    assert venda.status == 'cancelada'


def test_venda_com_parcelas_calcula_certo(app, admin_user, catalogo):
    """Venda com 2 parcelas — soma de parcelas igual ao total."""
    from app.extensions import db
    from app.models import EstoqueProducao
    from app.services import vendas_b2b as svc

    ep = EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=100)
    db.session.add(ep)
    db.session.commit()
    venda = svc.criar_venda(
        cliente_nome='X',
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 10, 'preco_unitario': 5.0}],
        parcelas=[
            {'vencimento': date.today(), 'valor': 25.0, 'forma_pagamento': 'pix'},
            {'vencimento': date.today() + timedelta(days=30), 'valor': 25.0},
        ],
        user=admin_user,
    )
    assert venda.valor_total == 50.0
    assert len(venda.parcelas) == 2


def test_receber_pagamento_parcial(app, admin_user, catalogo):
    """Pagamento parcial mantem em aberto; pagamento total quita."""
    from app.extensions import db
    from app.models import EstoqueProducao
    from app.services import vendas_b2b as svc

    ep = EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=10)
    db.session.add(ep)
    db.session.commit()
    venda = svc.criar_venda(
        cliente_nome='X',
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 5, 'preco_unitario': 20.0}],
        user=admin_user,
    )
    p = venda.parcelas[0]
    assert p.valor == 100.0
    svc.receber_pagamento(p, 30.0, forma_pagamento='pix')
    assert p.valor_pago == 30.0
    assert p.status == 'parcial'
    svc.receber_pagamento(p, 70.0, forma_pagamento='pix')
    assert p.valor_pago == 100.0
    assert p.status == 'pago'
    assert p.pago_em is not None


def test_preco_sugerido_com_desconto_cliente(app, catalogo):
    """Cliente com desconto_percentual aplica sobre preco atacado.
    Preco vem de Receita.preco_venda (atacado, igual /cardapio?tipo=atacado)."""
    from app.extensions import db
    from app.models import ClienteB2B
    from app.services.vendas_b2b import preco_sugerido

    catalogo['receita'].preco_venda = 10.0
    cli = ClienteB2B(nome='X', desconto_percentual=20)
    db.session.add(cli)
    db.session.commit()
    assert preco_sugerido(receita_id=catalogo['receita'].id) == 10.0
    assert preco_sugerido(receita_id=catalogo['receita'].id, cliente=cli) == 8.0


def test_preco_sugerido_produto_usa_preco_atacado(app, catalogo):
    """Pra Produto, le do campo preco_atacado existente."""
    from app.extensions import db
    from app.services.vendas_b2b import preco_sugerido

    catalogo['produto'].preco_atacado = 5.5
    db.session.commit()
    assert preco_sugerido(produto_id=catalogo['produto'].id) == 5.5

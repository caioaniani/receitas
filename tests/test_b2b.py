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


def test_criar_venda_persiste_observacao_item(app, admin_user, catalogo):
    """Observacao por item eh salva no VendaB2BItem."""
    from app.extensions import db
    from app.models import EstoqueProducao
    from app.services import vendas_b2b as svc

    db.session.add(EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=10))
    db.session.commit()
    venda = svc.criar_venda(
        cliente_nome='X',
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 2, 'preco_unitario': 5.0, 'observacao': 'fatiado'}],
        user=admin_user,
    )
    assert venda.itens[0].observacao == 'fatiado'


def test_editar_venda_ajusta_estoque(app, admin_user, catalogo):
    """Editar reduzindo a qtd devolve a diferenca ao EstoqueProducao."""
    from app.extensions import db
    from app.models import EstoqueProducao
    from app.services import vendas_b2b as svc

    ep = EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=20)
    db.session.add(ep)
    db.session.commit()
    venda = svc.criar_venda(
        cliente_nome='X',
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 5, 'preco_unitario': 10.0}],
        user=admin_user,
    )
    db.session.refresh(ep)
    assert ep.quantidade == 15
    svc.editar_venda(
        venda, cliente_nome='X',
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 2, 'preco_unitario': 10.0, 'observacao': 'fatiado'}],
        user=admin_user,
    )
    db.session.refresh(ep)
    assert ep.quantidade == 18  # devolveu 3 (5 -> 2)
    assert venda.valor_total == 20.0
    assert len(venda.itens) == 1
    assert venda.itens[0].quantidade == 2
    assert venda.itens[0].observacao == 'fatiado'


def test_editar_bloqueado_com_pagamento(app, admin_user, catalogo):
    """Venda com parcela paga nao pode ter itens editados (protege a receber)."""
    import pytest

    from app.extensions import db
    from app.models import EstoqueProducao
    from app.services import vendas_b2b as svc

    db.session.add(EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=20))
    db.session.commit()
    venda = svc.criar_venda(
        cliente_nome='X',
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 5, 'preco_unitario': 10.0}],
        user=admin_user,
    )
    svc.receber_pagamento(venda.parcelas[0], 10.0, forma_pagamento='pix')
    with pytest.raises(ValueError, match='pagamento'):
        svc.editar_venda(
            venda, cliente_nome='X',
            itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                    'quantidade': 2, 'preco_unitario': 10.0}],
            user=admin_user,
        )


def test_reabrir_e_ciclo_cancelar_nao_dobra_estoque(app, admin_user, catalogo):
    """criar -> cancelar -> reabrir -> cancelar mantem o estoque consistente."""
    from app.extensions import db
    from app.models import EstoqueProducao
    from app.services import vendas_b2b as svc

    ep = EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=10)
    db.session.add(ep)
    db.session.commit()
    venda = svc.criar_venda(
        cliente_nome='X',
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 4, 'preco_unitario': 5.0}],
        user=admin_user,
    )
    db.session.refresh(ep)
    assert ep.quantidade == 6
    svc.cancelar_venda(venda, user=admin_user)
    db.session.refresh(ep)
    assert ep.quantidade == 10
    svc.reabrir_venda(venda, user=admin_user)
    db.session.refresh(ep)
    assert ep.quantidade == 6
    assert venda.status == 'ativa'
    svc.cancelar_venda(venda, user=admin_user)
    db.session.refresh(ep)
    assert ep.quantidade == 10  # nao 14 (estorno por saldo, nao dobra)


def test_reverter_status_entrega(app, admin_user, catalogo):
    """Volta um passo no status de entrega; idempotente em 'pendente'."""
    from app.extensions import db
    from app.models import EstoqueProducao
    from app.services import vendas_b2b as svc

    db.session.add(EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=10))
    db.session.commit()
    venda = svc.criar_venda(
        cliente_nome='X',
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 1, 'preco_unitario': 5.0}],
        user=admin_user,
    )
    venda.status_entrega = 'separado'
    db.session.commit()
    svc.reverter_status_entrega(venda)
    assert venda.status_entrega == 'pendente'
    svc.reverter_status_entrega(venda)
    assert venda.status_entrega == 'pendente'


def test_cancelar_nao_estorna_venda_de_id_com_prefixo(app, admin_user, catalogo):
    """Regressao: cancelar #1 nao pode estornar movs de #10 (bug do LIKE '#1%')."""
    from app.extensions import db
    from app.models import EstoqueProducao
    from app.services import vendas_b2b as svc

    ep = EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=100)
    db.session.add(ep)
    # estoque farto pro produto das vendas-dummy (id 2..9) nao interferir em ep
    db.session.add(EstoqueProducao(produto_id=catalogo['produto'].id, quantidade=1000))
    db.session.commit()

    v1 = svc.criar_venda(
        cliente_nome='v1',
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 2, 'preco_unitario': 1.0}],
        user=admin_user,
    )
    vd = None
    while vd is None or vd.id < 10:
        vd = svc.criar_venda(
            cliente_nome='dummy',
            itens=[{'tipo': 'produto', 'id': catalogo['produto'].id,
                    'quantidade': 1, 'preco_unitario': 1.0}],
            user=admin_user,
        )
    # vd.id == 10: troca pra baixar 5 da MESMA receita de v1 (mesmo ep)
    svc.editar_venda(
        vd, cliente_nome='dummy',
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 5, 'preco_unitario': 1.0}],
        user=admin_user,
    )
    assert v1.id == 1 and vd.id == 10
    db.session.refresh(ep)
    assert ep.quantidade == 93  # 100 - 2 (#1) - 5 (#10)
    svc.cancelar_venda(v1, user=admin_user)
    db.session.refresh(ep)
    # fix: devolve so os 2 do #1 -> 95. bug ('#1%'): devolveria 2+5 -> 100.
    assert ep.quantidade == 95


def test_venda_editar_get_renderiza_form(app, admin_user, catalogo):
    """GET do form de edicao traz itens preenchidos + campo de observacao."""
    from app.extensions import db
    from app.models import EstoqueProducao
    from app.services import vendas_b2b as svc

    db.session.add(EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=20))
    db.session.commit()
    venda = svc.criar_venda(
        cliente_nome='Restaurante Z',
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 3, 'preco_unitario': 7.0, 'observacao': 'fatiado'}],
        user=admin_user,
    )
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    r = c.get(f'/b2b/vendas/{venda.id}/editar')
    assert r.status_code == 200
    assert b'item_obs[]' in r.data
    assert b'Salvar altera' in r.data
    assert b'fatiado' in r.data
    assert b'Restaurante Z' in r.data


def test_venda_editar_post_altera_estoque_e_obs(app, admin_user, catalogo):
    """POST de edicao reduz qtd, ajusta estoque e salva nova observacao."""
    from app.extensions import db
    from app.models import EstoqueProducao, VendaB2B
    from app.services import vendas_b2b as svc

    ep = EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=20)
    db.session.add(ep)
    db.session.commit()
    venda = svc.criar_venda(
        cliente_nome='Z',
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 5, 'preco_unitario': 10.0}],
        user=admin_user,
    )
    db.session.refresh(ep)
    assert ep.quantidade == 15
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    r = c.post(f'/b2b/vendas/{venda.id}/editar', data={
        'cliente_nome': 'Z',
        'data_venda': venda.data_venda.isoformat(),
        'data_entrega': venda.data_venda.isoformat(),
        'item_ref[]': f'receita:{catalogo["receita"].id}',
        'item_qtd[]': '2',
        'item_preco[]': '10.0',
        'item_desc[]': '',
        'item_estado[]': '',
        'item_obs[]': 'bem fatiado',
    })
    assert r.status_code in (302, 303)
    db.session.expire_all()
    v = db.session.get(VendaB2B, venda.id)
    assert len(v.itens) == 1
    assert v.itens[0].quantidade == 2
    assert v.itens[0].observacao == 'bem fatiado'
    assert db.session.get(EstoqueProducao, ep.id).quantidade == 18


def test_venda_reabrir_e_status_voltar_routes(app, admin_user, catalogo):
    """Rotas: reabrir (cancelada->ativa, re-baixa) e voltar status de entrega."""
    from app.extensions import db
    from app.models import EstoqueProducao, VendaB2B
    from app.services import vendas_b2b as svc

    ep = EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=10)
    db.session.add(ep)
    db.session.commit()
    venda = svc.criar_venda(
        cliente_nome='Z',
        itens=[{'tipo': 'receita', 'id': catalogo['receita'].id,
                'quantidade': 2, 'preco_unitario': 5.0}],
        user=admin_user,
    )
    svc.cancelar_venda(venda, user=admin_user)
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    c.post(f'/b2b/vendas/{venda.id}/reabrir')
    db.session.expire_all()
    assert db.session.get(VendaB2B, venda.id).status == 'ativa'
    assert db.session.get(EstoqueProducao, ep.id).quantidade == 8  # re-baixou

    v = db.session.get(VendaB2B, venda.id)
    v.status_entrega = 'separado'
    db.session.commit()
    c.post(f'/b2b/vendas/{venda.id}/status-voltar')
    db.session.expire_all()
    assert db.session.get(VendaB2B, venda.id).status_entrega == 'pendente'

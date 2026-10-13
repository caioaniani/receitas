"""Pote/cesta pedido à indústria movimenta os COMPONENTES (dono, 13/10/2026).

Caso da TV ("Está dizendo que não tem mas tem"): "Granola 50g" e "Molho
Pesto 600ml" apareciam em "Saíram sem produção lançada" com saldo 0 — a
saída debitava a linha do PRÓPRIO produto, que nenhuma produção credita,
enquanto a granola produzida (em gramas) ficava parada. A loja recebia a
linha do produto e a venda baixava os componentes. Agora a indústria
debita e a loja recebe o que o pote leva por dentro (mesma composição da
venda); cesta com fator não inteiro segue pela linha do produto.
"""
from datetime import date, timedelta
from unittest.mock import patch

from app.extensions import db
from app.models import (
    EstoqueLoja,
    EstoqueProducao,
    FaltaSaidaIndustria,
    Loja,
    MateriaPrima,
    MovEstoqueLoja,
    MovEstoqueProducao,
    MovimentacaoEstoque,
    PedidoItem,
    PedidoLoja,
    Produto,
    ProdutoItem,
    Receita,
)
from app.services import pedido_estoque as pe

_FOTO = [{'imagem': b'\xff\xd8\xff\xfake-jpeg', 'mimetype': 'image/jpeg'}]


def _base():
    loja = Loja(nome='Loja Pote', ativa=True, endereco='Rua P, 1')
    gran = Receita(nome='Produção - Granola', categoria='Granola',
                   rendimento_qtd=1000, rendimento_unidade='g', peso_base=1000.0,
                   peso_unitario=1.0)
    iog = Receita(nome='Produção - Iogurte', categoria='Iogurte',
                  rendimento_qtd=1000, rendimento_unidade='ml', peso_base=1000.0,
                  peso_unitario=1.0)
    pesto = Receita(nome='Molho pesto', categoria='molhos', rendimento_qtd=13,
                    rendimento_unidade='unidades', peso_base=1000.0)
    pote = MateriaPrima(nome='Pote de vidro pequeno', unidade='un',
                        custo_por_kg=1, estoque_atual=10)
    db.session.add_all([loja, gran, iog, pesto, pote])
    db.session.flush()
    g50 = Produto(nome='Granola 50g', categoria='Granola')
    i200 = Produto(nome='Iogurte 200ml', categoria='Iogurte')
    p600 = Produto(nome='Molho Pesto 600ml', categoria='Molhos')
    db.session.add_all([g50, i200, p600])
    db.session.flush()
    db.session.add_all([
        ProdutoItem(produto_id=g50.id, tipo='receita', receita_id=gran.id,
                    item_nome=gran.nome, quantidade=50),
        ProdutoItem(produto_id=i200.id, tipo='receita', receita_id=iog.id,
                    item_nome=iog.nome, quantidade=200),
        ProdutoItem(produto_id=i200.id, tipo='mp', materia_prima_id=pote.id,
                    item_nome=pote.nome, quantidade=1),
        ProdutoItem(produto_id=p600.id, tipo='receita', receita_id=pesto.id,
                    item_nome=pesto.nome, quantidade=0.6),
        EstoqueProducao(receita_id=gran.id, quantidade=2000),
        EstoqueProducao(receita_id=iog.id, quantidade=1000),
    ])
    db.session.commit()
    return dict(loja=loja, gran=gran, iog=iog, pesto=pesto, pote=pote,
                g50=g50, i200=i200, p600=p600)


def _pedido(loja, itens, status='separado'):
    p = PedidoLoja(loja_id=loja.id, status=status,
                   data_entrega=date.today() + timedelta(days=1))
    db.session.add(p)
    db.session.flush()
    for prod, qtd in itens:
        db.session.add(PedidoItem(pedido_id=p.id, produto_id=prod.id,
                                  quantidade=qtd))
    db.session.commit()
    return p


def _ep(**kw):
    return EstoqueProducao.query.filter_by(**kw).first()


def _el(loja, **kw):
    return EstoqueLoja.query.filter_by(loja_id=loja.id, **kw).first()


def test_composicao_do_item(app):
    b = _base()
    p = _pedido(b['loja'], [(b['g50'], 1), (b['i200'], 1), (b['p600'], 1)])
    g, i, m = sorted(p.itens, key=lambda x: x.id)
    assert pe.composicao_do_item(g) == [('receita_id', b['gran'].id, b['gran'].nome, 50)]
    assert sorted(pe.composicao_do_item(i)) == sorted([
        ('receita_id', b['iog'].id, b['iog'].nome, 200),
        ('materia_prima_id', b['pote'].id, b['pote'].nome, 1)])
    # Fator 0,6: segue pela linha do produto (saldo inteiro, sem inventar).
    assert pe.composicao_do_item(m) == [('produto_id', b['p600'].id, None, 1)]
    assert pe.item_explodido(g) and not pe.item_explodido(m)


def test_saida_debita_componentes_e_falta_vai_para_a_receita(app, admin_user):
    from app.blueprints.pedidos.routes import _executar_envio_pedido
    b = _base()
    p = _pedido(b['loja'], [(b['g50'], 30), (b['i200'], 6)])
    with app.test_request_context():
        ok, msg = _executar_envio_pedido(p, admin_user)
    assert ok is True
    # 30 × 50 g = 1500 g de granola; 6 × 200 ml = 1200 ml de iogurte (só 1000).
    assert _ep(receita_id=b['gran'].id).quantidade == 500
    assert _ep(receita_id=b['iog'].id).quantidade == 0
    assert db.session.get(MateriaPrima, b['pote'].id).estoque_atual == 4
    # Nenhuma linha do próprio pote é criada nem debitada.
    assert _ep(produto_id=b['g50'].id) is None and _ep(produto_id=b['i200'].id) is None
    falta = FaltaSaidaIndustria.query.filter_by(pedido_id=p.id).one()
    assert falta.receita_id == b['iog'].id and falta.quantidade == 200
    assert 'Produção - Iogurte' in msg
    mov = MovEstoqueProducao.query.filter_by(tipo='saida_pedido').filter(
        MovEstoqueProducao.referencia.like('%Granola 50g ×30%')).one()
    assert mov.quantidade == 1500 and mov.referencia.startswith(f'Pedido #{p.id} →')


def test_estorno_da_saida_devolve_componentes(app, admin_user):
    from app.blueprints.pedidos.routes import (
        _aplicar_voltar_status,
        _executar_envio_pedido,
    )
    b = _base()
    p = _pedido(b['loja'], [(b['g50'], 10), (b['i200'], 2)])
    with app.test_request_context():
        _executar_envio_pedido(p, admin_user)
    db.session.commit()
    assert _aplicar_voltar_status(p, admin_user.id) == ('em_transporte', 'separado')
    db.session.commit()
    assert _ep(receita_id=b['gran'].id).quantidade == 2000
    assert _ep(receita_id=b['iog'].id).quantidade == 1000
    assert db.session.get(MateriaPrima, b['pote'].id).estoque_atual == 10


def test_recebimento_credita_componentes_na_loja_e_estorno_desfaz(app, admin_user):
    from app.blueprints.pedidos.routes import (
        _aplicar_voltar_status,
        _executar_recebimento_pedido,
    )
    b = _base()
    p = _pedido(b['loja'], [(b['g50'], 30), (b['i200'], 6), (b['p600'], 2)],
                status='em_transporte')
    i200 = next(i for i in p.itens if i.produto_id == b['i200'].id)
    with patch('app.services.dropbox_storage.disponivel', return_value=False):
        ok, _msg, div = _executar_recebimento_pedido(
            p, admin_user, recebidos_map={i200.id: 5}, fotos=_FOTO)
    assert ok is True and div
    loja = b['loja']
    assert _el(loja, receita_id=b['gran'].id).quantidade == 1500
    assert _el(loja, receita_id=b['iog'].id).quantidade == 1000      # 5 recebidos
    assert _el(loja, materia_prima_id=b['pote'].id).quantidade == 5
    assert _el(loja, produto_id=b['p600'].id).quantidade == 2        # regime antigo
    assert _el(loja, produto_id=b['g50'].id) is None

    assert _aplicar_voltar_status(p, admin_user.id) == ('entregue', 'em_transporte')
    db.session.commit()
    assert _el(loja, receita_id=b['gran'].id).quantidade == 0
    assert _el(loja, receita_id=b['iog'].id).quantidade == 0
    assert _el(loja, materia_prima_id=b['pote'].id).quantidade == 0
    assert _el(loja, produto_id=b['p600'].id).quantidade == 0
    assert all(i.quantidade_recebida is None for i in p.itens)


def test_venda_do_pote_consome_o_que_o_recebimento_creditou(app, admin_user):
    """A loja passa a falar a língua da venda: vender 30 potes zera o que
    chegou, sem saída 'sem estoque'."""
    from app.blueprints.pedidos.routes import _executar_recebimento_pedido
    from app.services.baixa_venda import aplicar_venda
    b = _base()
    p = _pedido(b['loja'], [(b['g50'], 30)], status='em_transporte')
    with patch('app.services.dropbox_storage.disponivel', return_value=False):
        assert _executar_recebimento_pedido(p, admin_user, fotos=_FOTO)[0]
    aplicar_venda(b['loja'].id, produto_id=b['g50'].id, qtd=30, canal='site',
                  referencia='Venda teste', pedido_ref='venda-teste')
    db.session.commit()
    assert _el(b['loja'], receita_id=b['gran'].id).quantidade == 0
    assert MovEstoqueLoja.query.filter(
        MovEstoqueLoja.tipo.like('%sem_estoque%')).count() == 0


def test_estorno_loja_de_pedido_recebido_antes_da_mudanca(app, admin_user):
    """Pedido recebido no regime antigo (linha do produto) estorna a linha
    do produto pelos movimentos — não procura componentes."""
    from app.blueprints.pedidos.routes import _aplicar_voltar_status
    b = _base()
    p = _pedido(b['loja'], [(b['g50'], 4)], status='entregue')
    it = p.itens[0]
    it.quantidade_recebida = 4
    el = EstoqueLoja(loja_id=b['loja'].id, produto_id=b['g50'].id, quantidade=9)
    db.session.add(el)
    db.session.flush()
    db.session.add(MovEstoqueLoja(estoque_loja_id=el.id, tipo='entrada_pedido',
                                  quantidade=4, referencia=f'Pedido #{p.id}'))
    db.session.commit()
    _aplicar_voltar_status(p, admin_user.id)
    db.session.commit()
    assert el.quantidade == 5
    assert _el(b['loja'], receita_id=b['gran'].id) is None


def test_auditoria_reconhece_os_dois_regimes(app, admin_user):
    from app.blueprints.pedidos.routes import _executar_envio_pedido
    from app.services.auditoria_baixa_pedidos import auditar
    b = _base()
    novo = _pedido(b['loja'], [(b['g50'], 10), (b['i200'], 1)])
    with app.test_request_context():
        _executar_envio_pedido(novo, admin_user)
    # Pedido antigo: saiu debitando a linha do próprio pote.
    velho = _pedido(b['loja'], [(b['g50'], 3)], status='em_transporte')
    ep = EstoqueProducao(produto_id=b['g50'].id, quantidade=0)
    db.session.add(ep)
    db.session.flush()
    db.session.add(MovEstoqueProducao(
        estoque_producao_id=ep.id, tipo='saida_pedido_sem_estoque', quantidade=3,
        referencia=pe.referencia_saida(velho)))
    db.session.commit()
    res = auditar(dias=7)
    assert res['resumo']['divergente'] == 0 and res['resumo']['sem_movimento'] == 0
    assert MovimentacaoEstoque.query.filter(
        MovimentacaoEstoque.referencia.like(f'Pedido #{novo.id} →%')).one().quantidade == 1

"""Produtos simples participam da reposicao sem colidir com receitas e MPs."""
from datetime import datetime, time, timedelta

import pytest

from app.extensions import db
from app.models import (
    EstoqueLoja,
    Loja,
    MateriaPrima,
    MovEstoqueLoja,
    PedidoItem,
    PedidoLoja,
    Produto,
    ProdutoItem,
    Receita,
)
from app.services.auto_pedidos import gerar_pedidos_automaticos
from app.services.pedido_merge import MARCADOR_RASCUNHO_AUTO
from app.services.previsao_producao import sugerir_pedidos_por_venda
from app.utils import hoje


@pytest.fixture(autouse=True)
def segunda_18h(congela_hoje):
    congela_hoje(hora=18)


def _produto(loja, *, nome='Iogurte comprado', estoque=0, reserva=0,
             minimo=0, diario=0, fresco=False, ativo=True):
    produto = Produto(nome=nome, ativo=ativo)
    db.session.add(produto)
    db.session.flush()
    saldo = EstoqueLoja(
        loja_id=loja.id, produto_id=produto.id, quantidade=estoque,
        quantidade_reservada=reserva, estoque_minimo=minimo,
        pedido_minimo_diario=diario, reposicao_por_venda_diaria=fresco,
    )
    db.session.add(saldo)
    db.session.commit()
    return produto, saldo


def _mov(saldo, dia, tipo, quantidade):
    db.session.add(MovEstoqueLoja(
        estoque_loja_id=saldo.id, tipo=tipo, quantidade=quantidade,
        data=datetime.combine(dia, time(12)), referencia='regressao Produto'))


def _historico(saldo, *, venda=10, merma=0, tipo='venda_tiny'):
    for atraso in range(1, 43):
        dia = hoje() - timedelta(days=atraso)
        _mov(saldo, dia, tipo, venda)
        if merma:
            _mov(saldo, dia, 'perda', merma)
    db.session.commit()


def _pedido(loja, produto, dia, quantidade, *, status='pendente', auto=False,
            recebida=None):
    pedido = PedidoLoja(
        loja_id=loja.id, data_entrega=dia, status=status,
        observacao=MARCADOR_RASCUNHO_AUTO if auto else 'entrega existente')
    db.session.add(pedido)
    db.session.flush()
    db.session.add(PedidoItem(
        pedido_id=pedido.id, produto_id=produto.id, quantidade=quantidade,
        quantidade_recebida=recebida))
    db.session.commit()
    return pedido


def _grade(loja, **kw):
    params = {'inicio_offset_dias': 1, 'horizonte_dias': 2}
    params.update(kw)
    grade = sugerir_pedidos_por_venda(**params)
    return next((lj['produtos'] for lj in grade['lojas']
                 if lj['loja_id'] == loja.id), [])


def _linha(loja, produto, **kw):
    return next(p for p in _grade(loja, **kw)
                if p['item_key'] == f'prod:{produto.id}')


def test_produto_usa_reserva_venda_liquida_e_merma_ja_realizada(app, loja):
    produto, saldo = _produto(loja, estoque=20, reserva=8)
    _historico(saldo, merma=2)
    _mov(saldo, hoje(), 'venda_tiny', 10)
    _mov(saldo, hoje(), 'venda_tiny_estorno', 3)
    _mov(saldo, hoje(), 'perda', 2)
    db.session.commit()

    linha = _linha(loja, produto)
    assert linha['produto_id'] == produto.id
    assert linha['receita_id'] is None and linha['materia_prima_id'] is None
    assert linha['eh_produto'] is True and linha['eh_mp'] is False
    assert linha['estoque_atual'] == 12
    assert linha['por_dia'] == [3, 12]
    assert linha['lote'] == linha['minimo'] == 0
    assert linha['media_semanal'] == 70


def test_ids_coincidentes_nao_misturam_saldos_ou_minimos(app, loja):
    receita = Receita(id=701, nome='Receita', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100)
    mp = MateriaPrima(id=701, nome='MP', unidade='un', sugerir_pedido_loja=True)
    produto = Produto(id=701, nome='Produto', ativo=True)
    db.session.add_all([receita, mp, produto])
    db.session.flush()
    db.session.add_all([
        EstoqueLoja(loja_id=loja.id, receita_id=701, estoque_minimo=3),
        EstoqueLoja(loja_id=loja.id, materia_prima_id=701, estoque_minimo=5),
        EstoqueLoja(loja_id=loja.id, produto_id=701, estoque_minimo=7),
    ])
    db.session.commit()

    itens = {p['item_key']: p for p in _grade(loja, horizonte_dias=1)}
    assert {k: p['por_dia'] for k, p in itens.items()} == {
        '701': [3], 'mp:701': [5], 'prod:701': [7]}
    assert itens['701']['eh_produto'] is False
    assert itens['mp:701']['eh_produto'] is False


def test_produto_piso_diario_so_em_dias_abertos_com_estoque_alto(app, loja):
    loja = db.session.get(Loja, loja.id)
    loja.dias_funcionamento = '56'
    produto, _ = _produto(loja, estoque=100, diario=2)

    assert _linha(loja, produto, horizonte_dias=6)['por_dia'] == [0, 0, 0, 0, 2, 2]


def test_produto_fresco_usa_venda_sem_descontar_saldo_nem_repor_merma(app, loja):
    produto, saldo = _produto(loja, estoque=100, minimo=50, diario=2, fresco=True)
    _historico(saldo, venda=7, merma=3)

    linha = _linha(loja, produto)
    assert linha['reposicao_por_venda_diaria'] is True
    assert linha['por_dia'] == [7, 7]


def test_cesta_e_inativo_nao_entram_componentes_simples_continuam(app, loja):
    cesta, saldo_cesta = _produto(loja, nome='Cesta', diario=20)
    inativo, saldo_inativo = _produto(loja, nome='Inativo', diario=20, ativo=False)
    simples, _ = _produto(loja, nome='Componente simples', diario=2)
    db.session.add(ProdutoItem(
        produto_id=cesta.id, tipo='produto', produto_componente_id=simples.id,
        item_nome=simples.nome, quantidade=1))
    db.session.commit()
    _historico(saldo_cesta)
    _historico(saldo_inativo)

    assert [p['produto_id'] for p in _grade(loja)] == [simples.id]
    assert _linha(loja, simples)['por_dia'] == [2, 2]


@pytest.mark.parametrize('status', ['pendente', 'em_transporte'])
def test_produto_entrega_pendente_ou_em_transito_cobre_dias_seguinte(app, loja, status):
    produto, saldo = _produto(loja)
    _historico(saldo)
    _pedido(loja, produto, hoje(), 30, status=status)

    linha = _linha(loja, produto, inicio_offset_dias=0, horizonte_dias=4)
    assert linha['ja_pedido'] == [30, 0, 0, 0]
    assert linha['por_dia'] == [0, 0, 0, 10]
    assert _linha(loja, produto)['por_dia'] == [0, 0]


def test_produto_recebido_fica_visivel_sem_duplicar_saldo(app, loja):
    produto, saldo = _produto(loja, estoque=10)
    _historico(saldo)
    _pedido(loja, produto, hoje(), 10, status='recebido')

    linha = _linha(loja, produto, inicio_offset_dias=0)
    assert linha['ja_pedido'] == [10, 0]
    assert linha['por_dia'] == [0, 10]
    assert _linha(loja, produto, horizonte_dias=1)['por_dia'] == [10]


def test_produto_rascunho_ressincronizado_nao_credita_entrega_antiga(app, loja):
    produto, saldo = _produto(loja)
    _historico(saldo)
    amanha = hoje() + timedelta(days=1)
    _pedido(loja, produto, amanha, 30, auto=True)

    assert _linha(loja, produto)['por_dia'] == [0, 0]
    linha = _linha(loja, produto, ressincronizar_datas=[amanha])
    assert linha['por_dia'] == [10, 10]
    assert linha['ja_pedido'] == [0, 0]


def test_produto_entrega_parcial_no_historico_permite_teste_de_ruptura(app, loja):
    produto, saldo = _produto(loja, diario=2)
    _historico(saldo, venda=2)
    amanha = hoje() + timedelta(days=1)
    for semana in (1, 2, 3):
        _pedido(loja, produto, amanha - timedelta(days=7 * semana), 10,
                status='recebido', recebida=2)

    linha = _linha(loja, produto, horizonte_dias=1)
    assert linha['por_dia'] == [3]
    assert linha['teste_ruptura_dias'] == [amanha.isoformat()]


def test_produto_so_pedido_no_historico_aparece_sem_inventar_consumo(app, loja):
    produto, _ = _produto(loja)
    _pedido(loja, produto, hoje() - timedelta(days=7), 20, status='recebido')

    linha = _linha(loja, produto)
    assert linha['por_dia'] == [0, 0]
    assert linha['n_datas'] == 0


def test_auto_produto_cria_e_ressincroniza_pedido_real(app, loja, pedidos_antes_do_corte):
    produto, saldo = _produto(loja, diario=2)
    primeiro = gerar_pedidos_automaticos()
    amanha = hoje() + timedelta(days=1)
    pedido = PedidoLoja.query.filter_by(loja_id=loja.id, data_entrega=amanha).one()
    assert primeiro['criados'] == 6
    assert [(i.produto_id, i.receita_id, i.materia_prima_id, i.quantidade)
            for i in pedido.itens] == [(produto.id, None, None, 2)]

    saldo.pedido_minimo_diario = 5
    db.session.commit()
    segundo = gerar_pedidos_automaticos()
    db.session.refresh(pedido)
    assert segundo['criados'] == 0
    assert pedido.itens[0].quantidade == 5

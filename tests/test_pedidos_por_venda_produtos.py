"""Produto NUNCA entra no motor venda+estoque nem nos rascunhos automáticos
(decisão do dono 01/10/2026: "os pedidos das lojas para a indústria devem
ser somente do que a indústria produz, está vindo de tudo até coca cola e
adicional de morango").

SUBSTITUI o contrato anterior deste arquivo ("Produtos simples participam
da reposição"): a linha de EstoqueLoja de um Produto é revenda comprada
pronta (água, suco, adicional) ou cesta, e nenhuma das duas vira pedido —
mesmo com venda, estoque mínimo, piso diário ou modo fresco cadastrados.
Receita e MP liberada seguem como sempre."""
from datetime import datetime, time, timedelta

import pytest

from app.extensions import db
from app.models import (
    EstoqueLoja,
    MateriaPrima,
    MovEstoqueLoja,
    PedidoItem,
    PedidoLoja,
    Produto,
    ProdutoItem,
    Receita,
)
from app.services.auto_pedidos import gerar_pedidos_automaticos
from app.services.pedido_merge import OBSERVACAO_RASCUNHO_AUTO
from app.services.previsao_producao import sugerir_pedidos_por_venda
from app.utils import hoje


@pytest.fixture(autouse=True)
def segunda_18h(congela_hoje):
    congela_hoje(hora=18)


def _produto(loja, *, nome='Água com gás', estoque=0, minimo=0, diario=0,
             fresco=False, ativo=True):
    produto = Produto(nome=nome, ativo=ativo)
    db.session.add(produto)
    db.session.flush()
    saldo = EstoqueLoja(
        loja_id=loja.id, produto_id=produto.id, quantidade=estoque,
        estoque_minimo=minimo, pedido_minimo_diario=diario,
        reposicao_por_venda_diaria=fresco,
    )
    db.session.add(saldo)
    db.session.commit()
    return produto, saldo


def _receita(loja, *, nome='Pão controle', diario=0):
    receita = Receita(nome=nome, rendimento_qtd=1, rendimento_unidade='un',
                      peso_base=100)
    db.session.add(receita)
    db.session.flush()
    saldo = EstoqueLoja(loja_id=loja.id, receita_id=receita.id, quantidade=0,
                        pedido_minimo_diario=diario)
    db.session.add(saldo)
    db.session.commit()
    return receita, saldo


def _historico(saldo, *, venda=10, tipo='venda_tiny'):
    for atraso in range(1, 43):
        dia = hoje() - timedelta(days=atraso)
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=saldo.id, tipo=tipo, quantidade=venda,
            data=datetime.combine(dia, time(12)), referencia='regressao Produto'))
    db.session.commit()


def _grade(loja, **kw):
    params = {'inicio_offset_dias': 1, 'horizonte_dias': 2}
    params.update(kw)
    grade = sugerir_pedidos_por_venda(**params)
    return next((lj['produtos'] for lj in grade['lojas']
                 if lj['loja_id'] == loja.id), [])


def test_produto_de_revenda_com_venda_minimo_diario_e_fresco_nao_entra(app, loja):
    produto, saldo = _produto(loja, estoque=20, minimo=50, diario=2, fresco=True)
    _historico(saldo)

    assert [p for p in _grade(loja) if p.get('produto_id')] == []
    assert not any(p['item_key'].startswith('prod:') for p in _grade(loja))


def test_cesta_tambem_nao_entra_no_motor(app, loja):
    receita, _ = _receita(loja)
    cesta, saldo_cesta = _produto(loja, nome='Cesta de pão', diario=20)
    db.session.add(ProdutoItem(produto_id=cesta.id, tipo='receita',
                               receita_id=receita.id, item_nome=receita.nome,
                               quantidade=2))
    db.session.commit()
    _historico(saldo_cesta)

    assert [p for p in _grade(loja) if p.get('produto_id')] == []


def test_ids_coincidentes_receita_e_mp_seguem_produto_fica_fora(app, loja):
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
    assert {k: p['por_dia'] for k, p in itens.items()} == {'701': [3], 'mp:701': [5]}
    assert all(p['eh_produto'] is False and p['produto_id'] is None
               for p in itens.values())


def test_pedido_antigo_de_produto_nao_ressuscita_linha_na_grade(app, loja):
    """Histórico de pedido/entrega de Produto (dado real das lojas) não faz
    o item voltar à grade — nem o já-pedido do horizonte."""
    produto, saldo = _produto(loja)
    _historico(saldo)
    for dia, status in ((hoje() - timedelta(days=7), 'recebido'),
                        (hoje() + timedelta(days=1), 'confirmado')):
        pedido = PedidoLoja(loja_id=loja.id, data_entrega=dia, status=status,
                            observacao='entrega existente')
        db.session.add(pedido)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=pedido.id, produto_id=produto.id,
                                  quantidade=30))
    db.session.commit()

    assert [p for p in _grade(loja, inicio_offset_dias=0, horizonte_dias=4)
            if p.get('produto_id')] == []


def test_auto_pedidos_nao_cria_pedido_de_produto(app, loja, pedidos_antes_do_corte):
    produto, saldo = _produto(loja, diario=2)
    _historico(saldo)

    res = gerar_pedidos_automaticos()
    assert res['criados'] == 0
    assert PedidoItem.query.filter(PedidoItem.produto_id.isnot(None)).count() == 0


def test_auto_pedidos_remove_revenda_do_rascunho_pendente_do_cron(
        app, loja, pedidos_antes_do_corte):
    """Rascunho do próprio cron (sem carimbo humano) que já carregava água:
    a re-sincronização leva o item a zero (sai) e mantém a receita."""
    produto, _ = _produto(loja)
    receita, _ = _receita(loja, diario=2)
    amanha = hoje() + timedelta(days=1)
    pedido = PedidoLoja(loja_id=loja.id, data_entrega=amanha, status='pendente',
                        observacao=OBSERVACAO_RASCUNHO_AUTO)
    db.session.add(pedido)
    db.session.flush()
    db.session.add_all([
        PedidoItem(pedido_id=pedido.id, produto_id=produto.id, quantidade=9),
        PedidoItem(pedido_id=pedido.id, receita_id=receita.id, quantidade=2),
    ])
    db.session.commit()

    gerar_pedidos_automaticos()
    db.session.refresh(pedido)
    assert pedido.status == 'pendente'
    assert [(i.receita_id, i.produto_id, i.quantidade) for i in pedido.itens] == [
        (receita.id, None, 2)]
    assert pedido.modificado_por_id is None


def test_auto_pedidos_nao_mexe_em_pedido_confirmado_pela_loja(
        app, loja, admin_user, pedidos_antes_do_corte):
    """Pedido com carimbo humano é intocável pelo cron — a água que a loja
    confirmou fica; quem limpa é o dono em /admin/pedidos-revenda."""
    produto, _ = _produto(loja)
    receita, _ = _receita(loja, diario=2)
    amanha = hoje() + timedelta(days=1)
    pedido = PedidoLoja(loja_id=loja.id, data_entrega=amanha, status='confirmado',
                        observacao=OBSERVACAO_RASCUNHO_AUTO,
                        modificado_por_id=admin_user.id)
    db.session.add(pedido)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=pedido.id, produto_id=produto.id, quantidade=9))
    db.session.commit()

    gerar_pedidos_automaticos()
    db.session.refresh(pedido)
    assert [(i.produto_id, i.quantidade) for i in pedido.itens] == [(produto.id, 9)]

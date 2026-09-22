"""Reposicao usa o saldo atual e somente consumo/entregas ainda pendentes."""
from datetime import datetime, time, timedelta

import pytest

from app.extensions import db
from app.models import EstoqueLoja, Loja, MovEstoqueLoja, PedidoItem, PedidoLoja, Receita
from app.services.auto_pedidos import gerar_pedidos_automaticos
from app.services.previsao_producao import sugerir_pedidos_por_venda
from app.utils import hoje


def _cenario(estoque=0, especial=False, merma=0):
    loja = Loja(nome='Loja auditoria', ativa=True)
    receita = Receita(nome='Item auditoria', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100,
                      sugerir_pedido_loja=True, fornada_especial=especial)
    db.session.add_all([loja, receita])
    db.session.flush()
    saldo = EstoqueLoja(loja_id=loja.id, receita_id=receita.id,
                        quantidade=estoque)
    db.session.add(saldo)
    db.session.flush()
    for atraso in range(1, 43):
        d = hoje() - timedelta(days=atraso)
        if not especial or d.weekday() in (5, 6):
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=saldo.id, tipo='venda_seru', quantidade=10,
                data=datetime.combine(d, time(12)), referencia='auditoria'))
            if merma:
                db.session.add(MovEstoqueLoja(
                    estoque_loja_id=saldo.id, tipo='perda', quantidade=merma,
                    data=datetime.combine(d, time(12)), referencia='auditoria'))
    db.session.commit()
    return loja, receita, saldo


def _pedido(loja, receita, atraso=0, status='pendente', quantidade=10):
    pedido = PedidoLoja(loja_id=loja.id, status=status,
                        data_entrega=hoje() + timedelta(days=atraso),
                        data_pedido=hoje())
    db.session.add(pedido)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=pedido.id, receita_id=receita.id,
                              quantidade=quantidade))
    db.session.commit()


def _produto(loja, receita, offset, horizonte):
    grade = sugerir_pedidos_por_venda(
        horizonte_dias=horizonte, inicio_offset_dias=offset)
    lj = next(l for l in grade['lojas'] if l['loja_id'] == loja.id)
    return next(p for p in lj['produtos']
                if p['receita_id'] == receita.id)


def _dias(loja, receita, offset, horizonte):
    return _produto(loja, receita, offset, horizonte)['por_dia']


def _movimento_hoje(saldo, tipo, quantidade, hora=17):
    db.session.add(MovEstoqueLoja(
        estoque_loja_id=saldo.id, tipo=tipo, quantidade=quantidade,
        data=datetime.combine(hoje(), time(hora)), referencia='ja baixou saldo'))
    db.session.commit()


@pytest.mark.parametrize('status', ['recebido', 'entregue'])
def test_recebido_hoje_nao_pode_mudar_previsao_amanha_se_incluir_hoje_na_tela(
        app, congela_hoje, status):
    congela_hoje()
    loja, receita, _ = _cenario(estoque=10)
    _pedido(loja, receita, status=status, quantidade=10)
    a_partir_hoje = _dias(loja, receita, 0, 2)
    a_partir_amanha = _dias(loja, receita, 1, 1)
    assert a_partir_hoje[1] == a_partir_amanha[0] == 10
    assert _produto(loja, receita, 0, 2)['ja_pedido'] == [10, 0]


def test_entrega_sexta_de_especial_cobre_sabado_em_qualquer_janela(
        app, congela_hoje):
    congela_hoje()
    loja, receita, _ = _cenario(especial=True)
    _pedido(loja, receita, atraso=4, quantidade=10)
    semana = _dias(loja, receita, 1, 6)
    fim_semana = _dias(loja, receita, 5, 2)
    assert semana[4] == fim_semana[0] == 0


def test_venda_de_hoje_ja_baixada_nao_desconta_media_inteira_de_novo(
        app, congela_hoje):
    congela_hoje(hora=18)
    loja, receita, saldo = _cenario(estoque=10)
    # Comecou com 20, vendeu os 10 da media do dia e restam 10 para amanha.
    _movimento_hoje(saldo, 'venda_seru', 10)
    amanha = _dias(loja, receita, 1, 1)
    assert amanha == [0]
    assert _dias(loja, receita, 0, 2) == [0, 0]


def test_auto_especial_nao_deveria_gerar_de_novo_entrega_ja_agendada(
        app, congela_hoje):
    congela_hoje()
    loja, receita, _ = _cenario(especial=True)
    _pedido(loja, receita, atraso=4, quantidade=10)
    gerar_pedidos_automaticos()
    sabado = PedidoLoja.query.filter_by(
        loja_id=loja.id, data_entrega=hoje() + timedelta(days=5),
        status='pendente').first()
    qtd = sum(i.quantidade for i in sabado.itens) if sabado else 0
    assert qtd == 0


def test_auto_18h_nao_deveria_repor_novamente_venda_ja_baixada(
        app, congela_hoje):
    congela_hoje(hora=18)
    loja, receita, saldo = _cenario(estoque=10)
    _movimento_hoje(saldo, 'venda_seru', 10)
    gerar_pedidos_automaticos()
    amanha = PedidoLoja.query.filter_by(
        loja_id=loja.id, data_entrega=hoje() + timedelta(days=1),
        status='pendente').first()
    qtd = sum(i.quantidade for i in amanha.itens) if amanha else 0
    assert qtd == 0


@pytest.mark.parametrize('venda,estorno,quantidade_estorno', [
    ('venda_seru', 'venda_seru_estorno', 3),
    ('venda_tiny', 'venda_tiny_estorno', 3),
    ('venda_site', 'venda_site_estorno', -3),
    ('saida_lote', 'saida_lote_estorno', 3),
])
def test_so_venda_liquida_realizada_abate_consumo_restante(
        app, congela_hoje, venda, estorno, quantidade_estorno):
    congela_hoje(hora=18)
    loja, receita, saldo = _cenario(estoque=10)
    _movimento_hoje(saldo, venda, 10)
    _movimento_hoje(saldo, estorno, quantidade_estorno)

    # Realizado liquido 7 de 10; sobram 3 para hoje e 7 para amanha.
    assert _dias(loja, receita, 1, 1) == [3]
    assert _dias(loja, receita, 0, 2) == [0, 3]


@pytest.mark.parametrize('venda,perda,esperado', [
    (10, 4, 0),   # consumo previsto do dia ja ocorreu todo
    (15, 0, 4),   # venda acima da media nao quita perda ainda prevista
    (0, 8, 10),   # perda acima da media nao quita venda ainda prevista
])
def test_venda_e_merma_realizadas_abatem_suas_proprias_previsoes(
        app, congela_hoje, venda, perda, esperado):
    congela_hoje(hora=18)
    loja, receita, saldo = _cenario(estoque=14, merma=4)
    _movimento_hoje(saldo, 'venda_seru', venda)
    _movimento_hoje(saldo, 'perda', perda)

    assert _dias(loja, receita, 1, 1) == [esperado]


def test_estorno_liquido_negativo_nao_aumenta_previsao_do_dia(
        app, congela_hoje):
    congela_hoje(hora=18)
    loja, receita, saldo = _cenario(estoque=10)
    _movimento_hoje(saldo, 'venda_seru_estorno', 20)

    assert _dias(loja, receita, 1, 1) == [10]


def test_venda_alta_hoje_nao_altera_media_nem_credita_estoque_ficticio(
        app, congela_hoje):
    congela_hoje(hora=18)
    loja, receita, saldo = _cenario(estoque=0)
    _movimento_hoje(saldo, 'venda_seru_sem_estoque', 100)

    produto = _produto(loja, receita, 1, 7)
    assert produto['por_dia'] == [10] * 7
    assert produto['media_semanal'] == 70
    assert produto['n_datas'] == 42


@pytest.mark.parametrize('fresco,esperado', [(False, 2), (True, 10)])
def test_realizado_hoje_preserva_piso_diario_e_reposicao_fresca(
        app, congela_hoje, fresco, esperado):
    congela_hoje(hora=18)
    loja, receita, saldo = _cenario(estoque=100)
    saldo.pedido_minimo_diario = 2
    saldo.reposicao_por_venda_diaria = fresco
    _movimento_hoje(saldo, 'venda_seru', 10)

    assert _dias(loja, receita, 0, 2) == [esperado, esperado]


def test_hoje_recebido_e_pendente_mostra_ambos_credita_so_pendente(
        app, congela_hoje):
    congela_hoje()
    loja, receita, _ = _cenario(estoque=10)
    _pedido(loja, receita, status='recebido', quantidade=10)
    _pedido(loja, receita, status='pendente', quantidade=5)

    produto = _produto(loja, receita, 0, 2)
    assert produto['ja_pedido'] == [15, 0]
    assert produto['por_dia'] == [0, 5]
    assert _dias(loja, receita, 1, 1) == [5]

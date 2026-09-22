"""Previsão e detalhamento respeitam calendário sem esconder pedidos firmes."""
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models import EstoqueLoja, Loja, MovEstoqueLoja, PedidoItem, PedidoLoja, Receita
from app.services.previsao_producao import (
    balanco_industria,
    cronograma_producao,
    decompor_previsao,
    sugerir_pedidos_por_venda,
)
from app.utils import hoje


@pytest.mark.parametrize('ativa,abertura', [(True, '56'), (False, None)])
@pytest.mark.parametrize('motor', ['vendas', 'pedidos', 'maior'])
def test_industria_nao_preve_loja_sem_operacao(
        app, congela_hoje, ativa, abertura, motor):
    congela_hoje()
    loja = Loja(nome='Loja auditoria', ativa=ativa,
                dias_funcionamento=abertura)
    receita = Receita(nome='Choconana auditoria', categoria='Viennoiserie',
                      rendimento_qtd=1, rendimento_unidade='un', peso_base=100,
                      sugerir_pedido_loja=True)
    db.session.add_all([loja, receita])
    db.session.flush()
    saldo = EstoqueLoja(loja_id=loja.id, receita_id=receita.id, quantidade=0)
    db.session.add(saldo)
    db.session.flush()
    # Seis terças com 10 vendas, antes de fechar/mudar o calendário da loja.
    for semana in range(1, 7):
        dia = hoje() + timedelta(days=1 - 7 * semana)
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=saldo.id, tipo='venda_seru', quantidade=10,
            data=datetime.combine(dia, datetime.min.time()), referencia='auditoria'))
        pedido = PedidoLoja(loja_id=loja.id, data_entrega=dia, status='recebido')
        db.session.add(pedido)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=pedido.id, receita_id=receita.id, quantidade=10))
    db.session.commit()

    sugestao = sugerir_pedidos_por_venda(horizonte_dias=1, inicio_offset_dias=1)
    assert not any(sum(p['por_dia']) for l in sugestao['lojas']
                   for p in l['produtos'])
    balanco = balanco_industria(horizonte_dias=1, inicio_offset_dias=1,
                                motor=motor, usar_cache=False)
    assert not balanco['itens']
    cronograma = cronograma_producao(horizonte_dias=1, inicio_offset_dias=1,
                                    motor=motor, equilibrar=False)
    assert all(linha['total'] == 0 for linha in cronograma['receitas'])
    detalhe = decompor_previsao(receita.id, horizonte_dias=1,
                                inicio_offset_dias=1, motor=motor)
    assert detalhe['total_previsto'] == 0

    # Entrega excepcional explicitamente encomendada continua firme.
    pedido = PedidoLoja(loja_id=loja.id, data_entrega=hoje() + timedelta(days=1),
                        status='pendente')
    db.session.add(pedido)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=pedido.id, receita_id=receita.id, quantidade=7))
    db.session.commit()
    balanco = balanco_industria(horizonte_dias=1, inicio_offset_dias=1,
                                motor=motor, usar_cache=False)
    assert balanco['itens'][0]['previsto'] == 0
    assert balanco['itens'][0]['produzir'] == 7


def test_residual_da_cantina_nao_inflaciona_loja_diaria(app, congela_hoje):
    congela_hoje()
    receita = Receita(nome='Item misto', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100)
    diaria = Loja(nome='Diária', ativa=True)
    cantina = Loja(nome='Cantina', ativa=True, dias_funcionamento='56')
    db.session.add_all([receita, diaria, cantina])
    db.session.flush()
    for loja, quantidade in [(diaria, 4), (cantina, 42)]:
        saldo = EstoqueLoja(loja_id=loja.id, receita_id=receita.id, quantidade=0)
        db.session.add(saldo)
        db.session.flush()
        atrasos = range(1, 7) if loja == diaria else [1]
        for semana in atrasos:
            dia = hoje() + timedelta(days=1 - 7 * semana)
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=saldo.id, tipo='venda_seru', quantidade=quantidade,
                data=datetime.combine(dia, datetime.min.time()), referencia='misto'))
    db.session.commit()
    balanco = balanco_industria(horizonte_dias=1, inicio_offset_dias=1,
                                motor='vendas', usar_cache=False)
    assert balanco['itens'][0]['previsto'] == 4
    detalhe = decompor_previsao(receita.id, horizonte_dias=1,
                                inicio_offset_dias=1, motor='vendas')
    assert detalhe['total_previsto'] == 4
    assert {x['loja_nome'] for x in detalhe['dias'][0]['previsto_lojas']} == {'Diária'}


def test_estorno_de_outra_loja_nao_apaga_demanda_nem_diverge_do_detalhe(app, congela_hoje):
    congela_hoje()
    receita = Receita(nome='Item com estorno', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100)
    a, b = Loja(nome='Loja A', ativa=True), Loja(nome='Loja B', ativa=True)
    db.session.add_all([receita, a, b])
    db.session.flush()
    for loja, tipo, qtd in [(a, 'venda_seru', 10), (b, 'venda_seru_estorno', 20)]:
        saldo = EstoqueLoja(loja_id=loja.id, receita_id=receita.id, quantidade=0)
        db.session.add(saldo)
        db.session.flush()
        for semana in range(1, 7):
            dia = hoje() + timedelta(days=1 - 7 * semana)
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=saldo.id, tipo=tipo, quantidade=qtd,
                data=datetime.combine(dia, datetime.min.time()), referencia='estorno outra loja'))
    db.session.commit()
    balanco = balanco_industria(horizonte_dias=1, inicio_offset_dias=1,
                                motor='vendas', usar_cache=False)
    detalhe = decompor_previsao(receita.id, horizonte_dias=1,
                                inicio_offset_dias=1, motor='vendas')
    crono = cronograma_producao(horizonte_dias=1, inicio_offset_dias=1,
                               motor='vendas', equilibrar=False)
    assert balanco['itens'][0]['previsto'] == detalhe['total_previsto'] == 10
    assert next(r['total'] for r in crono['receitas'] if r['receita_id'] == receita.id) == 10

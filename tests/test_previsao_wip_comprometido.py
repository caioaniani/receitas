"""WIP que atende entregas anteriores nao fica livre para a janela futura."""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import (
    EstoqueProducao,
    Loja,
    PedidoItem,
    PedidoLoja,
    PlanejamentoItem,
    PlanejamentoProducao,
    Receita,
    ReceitaIngrediente,
)
from app.services.previsao_producao import balanco_industria, cronograma_producao
from app.utils import hoje


@pytest.fixture(autouse=True)
def _terca_fixa(congela_hoje):
    congela_hoje(2026, 9, 15)


def _pedido(loja, receita, dias, qtd):
    data = hoje() + timedelta(days=dias)
    pedido = PedidoLoja(loja_id=loja.id, status='pendente',
                        data_pedido=hoje(), data_entrega=data)
    db.session.add(pedido)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=pedido.id, receita_id=receita.id,
                              quantidade=qtd))


def _cenario(lead=0, nao_abate=False, wip=20):
    loja = Loja(nome='Loja WIP', ativa=True)
    receita = Receita(
        nome='Brioche', categoria='Paes', rendimento_qtd=1,
        rendimento_unidade='un', peso_base=1000, dias_producao=lead,
        lote_producao=10, antecedencia_max_dias=0, producao_max_dia=40,
        estoque_nao_abate=nao_abate)
    plano = PlanejamentoProducao(data=hoje(), origem='cronograma',
                                 enviado_ao_padeiro=True)
    db.session.add_all([loja, receita, plano])
    db.session.flush()
    item = PlanejamentoItem(planejamento_id=plano.id, receita_id=receita.id,
                            qtd_alvo=wip, produzido_qtd=0)
    db.session.add(item)
    # O fisico ignorado precisa continuar visivel, sem cobrir a demanda.
    if nao_abate:
        db.session.add(EstoqueProducao(receita_id=receita.id, quantidade=100))
    return loja, receita, item


def _calcular(receita, offset=1):
    db.session.commit()
    params = dict(horizonte_dias=3, inicio_offset_dias=offset)
    balanco = balanco_industria(**params, usar_cache=False)
    cronograma = cronograma_producao(**params, equilibrar=True)
    bal = next(x for x in balanco['itens'] if x['receita_id'] == receita.id)
    linha = next(x for x in cronograma['receitas'] if x['receita_id'] == receita.id)
    return bal, linha, cronograma


@pytest.mark.parametrize('lead', [0, 1])
@pytest.mark.parametrize('nao_abate', [False, True])
def test_wip_consumido_antes_da_janela_nao_reduz_novo_pedido(app, lead, nao_abate):
    loja, receita, item = _cenario(lead=lead, nao_abate=nao_abate)
    _pedido(loja, receita, lead, 20)
    _pedido(loja, receita, lead + 1, 40)

    bal, linha, _ = _calcular(receita)
    assert bal['comprometido'] == 40
    assert bal['comprometido_iminente'] == 20
    assert bal['em_producao'] == 20
    assert bal['em_producao_por_dia'] == {
        (hoje() + timedelta(days=lead)).isoformat(): 20}
    assert bal['em_estoque'] == (100 if nao_abate else 0)
    assert bal['em_estoque_efetivo'] == (100 if nao_abate else 0)
    assert bal['em_estoque_planejamento'] == 0
    assert bal['produzir'] == 40
    assert [c['qtd'] for c in linha['por_dia']] == [40, 0, 0]
    assert linha['produzir'] == 40
    assert linha['em_estoque'] == bal['em_estoque']
    assert linha['em_estoque_efetivo'] == 0
    assert linha['projecao'][lead]['saldo'] == 0
    assert linha['entregas_risco'] == []
    # Recalcular nao pode atualizar a ordem que o padeiro recebeu.
    db.session.refresh(item)
    assert item.qtd_alvo == 20
    assert item.produzido_qtd == 0
    assert item.planejamento.enviado_ao_padeiro is True


def test_wip_tardio_nao_apaga_deficit_nem_cobre_duas_entregas(app):
    loja, receita, _ = _cenario(lead=2)
    _pedido(loja, receita, 1, 20)  # WIP so fica pronto no dia seguinte.
    _pedido(loja, receita, 3, 40)

    bal, linha, _ = _calcular(receita)
    assert bal['em_estoque_planejamento'] == 0
    assert bal['produzir'] == 40
    assert linha['por_dia'][0]['qtd'] == 40
    assert linha['projecao'][0]['producao'] == 0
    assert linha['projecao'][0]['saldo'] == -20
    assert linha['projecao'][1]['producao'] == 20
    assert linha['projecao'][1]['saldo'] == 0
    assert linha['projecao'][2]['saldo'] == 0
    assert linha['entregas_risco'] == [{
        'data': (hoje() + timedelta(days=1)).isoformat(),
        'label': 'Qua 16/09', 'firme': 20, 'faltam': 20}]


def test_offset_zero_preserva_demanda_e_nao_desconta_ordem_da_grade(app):
    loja, receita, _ = _cenario()
    _pedido(loja, receita, 0, 20)
    _pedido(loja, receita, 1, 40)

    bal, linha, _ = _calcular(receita, offset=0)
    assert bal['em_producao'] == 0
    assert bal['em_producao_por_dia'] == {}
    assert bal['produzir'] == 60
    assert [c['qtd'] for c in linha['por_dia']] == [20, 40, 0]
    assert linha['entregas_risco'] == []


@pytest.mark.parametrize('nao_abate', [False, True])
@pytest.mark.parametrize('wip,disponivel,produzir', [(20, 0, 50), (50, 30, 20)])
def test_bom_usa_so_sobra_do_wip_apos_entregas_proprias(
        app, nao_abate, wip, disponivel, produzir):
    loja, receita, _ = _cenario(lead=1, nao_abate=nao_abate, wip=wip)
    montagem = Receita(nome='Montagem WIP', categoria='Montagens',
                        rendimento_qtd=1, rendimento_unidade='un', peso_base=1000,
                        antecedencia_max_dias=0)
    db.session.add(montagem)
    db.session.flush()
    db.session.add(ReceitaIngrediente(
        receita_id=montagem.id, tipo='receita', sub_receita_id=receita.id,
        ingrediente_nome=receita.nome, porcentagem=1))
    _pedido(loja, receita, 1, 20)  # Consome WIP antes da janela produtivel.
    _pedido(loja, receita, 2, 10)  # Demanda propria tambem reserva saldo.
    _pedido(loja, montagem, 3, 40)

    bal, linha, cronograma = _calcular(receita)
    pai = next(r for r in cronograma['receitas'] if r['receita_id'] == montagem.id)
    assert bal['em_producao'] == wip
    assert bal['em_estoque_planejamento'] == disponivel
    assert bal['produzir'] == (10 if wip == 20 else 0)
    assert pai['total'] == 40
    assert linha['consumo_janela'] == 40
    assert linha['total'] == produzir
    assert [c['qtd'] for c in linha['por_dia']] == (
        [10, 40, 0] if wip == 20 else [0, 20, 0])

"""Balanço, agenda e detalhamento enxergam as mesmas encomendas e vendas B2B."""
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models import (
    CronogramaOverride,
    EstoqueLoja,
    EstoqueProducao,
    Loja,
    MovEstoqueLoja,
    PedidoItem,
    PedidoLoja,
    PedidoOnline,
    PedidoOnlineItem,
    PedidoOnlineItemComponente,
    PlanejamentoItem,
    PlanejamentoProducao,
    Produto,
    ProdutoItem,
    Receita,
    ReceitaIngrediente,
    VendaB2B,
    VendaB2BItem,
)
from app.services.previsao_producao import (
    balanco_industria,
    cronograma_producao,
    decompor_previsao,
)
from app.utils import agora, hoje


@pytest.fixture(autouse=True)
def _segunda_fixa(congela_hoje):
    congela_hoje(2026, 9, 7)


def _receita(lead=0):
    rec = Receita(nome='Mini Pain', categoria='Viennoiserie',
                  rendimento_qtd=1, rendimento_unidade='un', peso_base=100,
                  sob_encomenda=True, dias_producao=lead)
    db.session.add(rec)
    db.session.flush()
    return rec


def _pedido(rec, origem, qtd=50, status=None, dias=2, produto=None):
    entrega = hoje() + timedelta(days=dias)
    alvo = {'produto_id': produto.id} if produto else {'receita_id': rec.id}
    if origem == 'site':
        pedido = PedidoOnline(
            codigo=f'TEST{PedidoOnline.query.count()}', nome_cliente='Teste',
            email_cliente='teste@example.test', modo_entrega='retirada',
            status=status or 'pago', data_entrega=entrega)
        db.session.add(pedido)
        db.session.flush()
        item = PedidoOnlineItem(
            pedido_id=pedido.id, kind='produto' if produto else 'receita',
            nome=produto.nome if produto else rec.nome,
            quantidade=qtd, preco_unitario=8, subtotal=qtd * 8, **alvo)
    else:
        pedido = VendaB2B(cliente_nome='Teste', status=status or 'ativa',
                          data_entrega=entrega)
        db.session.add(pedido)
        db.session.flush()
        item = VendaB2BItem(venda_id=pedido.id, quantidade=qtd,
                            preco_unitario=8, **alvo)
    db.session.add(item)
    db.session.commit()
    return pedido, item


@pytest.mark.parametrize('origem,nome', [('site', 'Encomenda site'),
                                       ('b2b', 'Vendas B2B')])
@pytest.mark.parametrize('lead', [0, 2])
def test_firme_sem_historico_agenda_producao_e_chega_ao_padeiro(
        app, admin_user, origem, nome, lead):
    """Regressão: balanço dizia produzir 50 e o cronograma mandava zero."""
    from app.services.producao import enviar_plano_do_dia

    rec = _receita(lead=lead)
    _pedido(rec, origem)
    bal = balanco_industria(usar_cache=False, motor='vendas')['itens'][0]
    crono = cronograma_producao(motor='vendas')
    linha = next(r for r in crono['receitas'] if r['receita_id'] == rec.id)
    detalhe = decompor_previsao(rec.id, motor='vendas')

    assert bal['comprometido'] == bal['produzir'] == linha['total'] == 50
    assert linha['por_dia'][2 - lead]['qtd'] == 50
    assert linha['projecao'][2]['saida_firme'] == 50
    assert linha['projecao'][2]['saida_lojas'] == [{'loja_nome': nome, 'qtd': 50}]
    assert linha['entregas_risco'] == []
    assert detalhe['total_firme'] == 50
    assert detalhe['dias'][2 - lead]['firme_lojas'] == [
        {'loja_nome': nome, 'qtd': 50}]

    plano = enviar_plano_do_dia(hoje() + timedelta(days=2 - lead),
                                user_id=admin_user.id, crono=crono)
    assert plano.enviado_ao_padeiro is True
    assert [(i.receita_id, i.qtd_alvo) for i in plano.itens] == [(rec.id, 50)]


@pytest.mark.parametrize('origem', ['site', 'b2b'])
def test_entrega_firme_sem_cobertura_aparece_no_alerta(app, origem):
    rec = _receita()
    _pedido(rec, origem)
    db.session.add(CronogramaOverride(
        receita_id=rec.id, data=hoje() + timedelta(days=2), qtd=0))
    db.session.commit()

    crono = cronograma_producao()
    linha = next(r for r in crono['receitas'] if r['receita_id'] == rec.id)
    assert linha['total'] == 0
    assert linha['entregas_risco'][0]['faltam'] == 50
    assert crono['alertas_falta'][0]['receita_id'] == rec.id


def test_tres_origens_compartilham_estoque_sem_perder_entregas(app):
    rec = _receita()
    _pedido(rec, 'site', qtd=10, dias=1)
    _pedido(rec, 'b2b', qtd=20, dias=2)
    loja = Loja(nome='Loja Teste', ativa=True)
    db.session.add(loja)
    db.session.flush()
    pedido = PedidoLoja(loja_id=loja.id, status='pendente',
                        data_entrega=hoje() + timedelta(days=3))
    db.session.add(pedido)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=pedido.id, receita_id=rec.id, quantidade=30))
    db.session.add(EstoqueProducao(receita_id=rec.id, quantidade=15))
    db.session.commit()

    bal = balanco_industria(usar_cache=False)['itens'][0]
    linha = cronograma_producao()['receitas'][0]
    assert bal['comprometido'] == 60
    assert bal['produzir'] == linha['total'] == 45
    assert [c['qtd'] for c in linha['por_dia']] == [0, 0, 15, 30, 0, 0, 0]
    assert [d['saida_firme'] for d in linha['projecao']] == [0, 10, 20, 30, 0, 0, 0]
    assert linha['entregas_risco'] == []


@pytest.mark.parametrize('origem', ['site', 'b2b'])
def test_cesta_explode_receitas_e_site_respeita_composicao_escolhida(app, origem):
    rec = _receita()
    cesta = Produto(nome='Caixa de Minis', ativo=True, sob_encomenda=True)
    db.session.add(cesta)
    db.session.flush()
    padrao = ProdutoItem(produto_id=cesta.id, tipo='receita',
                         receita_id=rec.id, item_nome=rec.nome, quantidade=5)
    db.session.add(padrao)
    db.session.flush()
    _cabecalho, item = _pedido(rec, origem, produto=cesta, qtd=2)
    if origem == 'site':
        db.session.add(PedidoOnlineItemComponente(
            item_id=item.id, produto_item_id=padrao.id, tipo='receita',
            receita_id=rec.id, nome=rec.nome, quantidade=7))
        db.session.commit()

    esperado = 14 if origem == 'site' else 10
    bal = balanco_industria(usar_cache=False)['itens'][0]
    linha = cronograma_producao()['receitas'][0]
    assert bal['comprometido'] == linha['total'] == esperado
    assert linha['por_dia'][2]['qtd'] == esperado


@pytest.mark.parametrize('status', ['aguardando_pagamento', 'cancelado', 'entregue'])
def test_site_sem_demanda_ativa_nao_entra_na_agenda(app, status):
    rec = _receita()
    _pedido(rec, 'site', status=status)
    linha = cronograma_producao()['receitas'][0]
    assert linha['total'] == linha['comprometido'] == 0
    assert linha['entregas_risco'] == []


def test_site_normal_e_b2b_separado_nao_entram_na_agenda(app):
    rec = _receita()
    rec.sob_encomenda = False
    _pedido(rec, 'site')
    b2b, _item = _pedido(rec, 'b2b')
    b2b.estoque_baixado_em = agora()
    db.session.commit()

    linha = cronograma_producao()['receitas'][0]
    assert linha['total'] == linha['comprometido'] == 0


def test_divulgacao_sob_encomenda_tem_producao(app):
    rec = _receita()
    _pedido(rec, 'site', status='divulgacao')
    linha = cronograma_producao()['receitas'][0]
    assert linha['por_dia'][2]['qtd'] == 50


def _historico_loja(rec, dow_offset=2):
    """Os dois motores históricos recebem o mesmo sinal de 100 por semana."""
    loja = Loja(nome='Loja Histórico', ativa=True)
    db.session.add(loja)
    db.session.flush()
    el = EstoqueLoja(loja_id=loja.id, receita_id=rec.id, quantidade=0)
    db.session.add(el)
    db.session.flush()
    for semana in (1, 2):
        dia = hoje() + timedelta(days=dow_offset - 7 * semana)
        pedido = PedidoLoja(loja_id=loja.id, status='entregue', data_entrega=dia)
        db.session.add(pedido)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=pedido.id, receita_id=rec.id,
                                  quantidade=100))
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo='venda_seru', quantidade=100,
            data=datetime.combine(dia, datetime.min.time())))
    db.session.commit()
    return loja


@pytest.mark.parametrize('motor', ['vendas', 'pedidos', 'maior'])
@pytest.mark.parametrize('origem', ['site', 'b2b'])
@pytest.mark.parametrize('lead,estoque', [(0, 0), (2, 25)])
def test_encomenda_soma_a_previsao_das_lojas(app, motor, origem, lead, estoque):
    """100 para lojas + 50 vendidos fora delas: o max não pode apagar os 50."""
    rec = _receita(lead=lead)
    _historico_loja(rec)
    _pedido(rec, origem)
    db.session.add(EstoqueProducao(receita_id=rec.id, quantidade=estoque))
    db.session.commit()

    bal = balanco_industria(usar_cache=False, motor=motor, horizonte_dias=3)['itens'][0]
    crono = cronograma_producao(motor=motor, horizonte_dias=3)
    linha = crono['receitas'][0]
    detalhe = decompor_previsao(rec.id, motor=motor, horizonte_dias=3)
    assert bal['previsto'] == 100
    assert bal['comprometido'] == 50
    assert bal['demanda'] == detalhe['total_demanda'] == 150
    assert bal['produzir'] == linha['total'] == 150 - estoque
    assert linha['por_dia'][2 - lead]['qtd'] == 150 - estoque
    assert linha['projecao'][2]['saida'] == 150
    assert linha['projecao'][2]['saldo'] == 0
    assert detalhe['dias'][2 - lead]['firme_adicional'] == 50
    assert detalhe['dias'][2 - lead]['demanda'] == 150


@pytest.mark.parametrize('origem', ['site', 'b2b'])
def test_demanda_iminente_adicional_abate_estoque_antes_do_horizonte(app, origem):
    rec = _receita(lead=2)
    loja = _historico_loja(rec, dow_offset=1)
    _pedido(rec, origem, dias=1)
    pedido = PedidoLoja(loja_id=loja.id, status='pendente',
                        data_entrega=hoje() + timedelta(days=3))
    db.session.add(pedido)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=pedido.id, receita_id=rec.id, quantidade=100))
    db.session.add(EstoqueProducao(receita_id=rec.id, quantidade=200))
    db.session.commit()

    bal = balanco_industria(usar_cache=False, horizonte_dias=3)['itens'][0]
    assert bal['em_estoque_efetivo'] == 50  # 200 - (lojas100 + encomenda50)
    assert bal['produzir'] == 50  # futura entrega100 menos estoque livre50
    assert cronograma_producao(horizonte_dias=3)['receitas'][0]['total'] == 50


def test_insumo_vendido_nao_reutiliza_estoque_da_demanda_propria(app):
    sub = _receita()
    sub.nome = 'Creme'
    loja = _historico_loja(sub, dow_offset=3)
    pai = _receita()
    pai.nome = 'Doce com creme'
    db.session.add(ReceitaIngrediente(
        receita_id=pai.id, tipo='receita', sub_receita_id=sub.id,
        ingrediente_nome=sub.nome, porcentagem=1))
    for rec, dias, qtd in ((sub, 1, 200), (pai, 2, 50)):
        pedido = PedidoLoja(loja_id=loja.id, status='pendente',
                            data_entrega=hoje() + timedelta(days=dias))
        db.session.add(pedido)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=pedido.id, receita_id=rec.id,
                                  quantidade=qtd))
    db.session.add(EstoqueProducao(receita_id=sub.id, quantidade=250))
    db.session.commit()

    crono = cronograma_producao()
    linha = next(r for r in crono['receitas'] if r['receita_id'] == sub.id)
    assert linha['demanda'] == 300
    assert linha['consumo_janela'] == 50
    assert linha['total'] == 100  # demanda própria300 + insumo50 - estoque250


def test_janela_futura_nao_reutiliza_estoque_consumido_antes(app):
    rec = _receita()
    _pedido(rec, 'site', dias=1)
    _pedido(rec, 'site', dias=3)
    db.session.add(EstoqueProducao(receita_id=rec.id, quantidade=50))
    db.session.add(CronogramaOverride(
        receita_id=rec.id, data=hoje() + timedelta(days=3), qtd=0))
    db.session.commit()

    bal = balanco_industria(usar_cache=False, inicio_offset_dias=2)['itens'][0]
    linha = cronograma_producao(inicio_offset_dias=2)['receitas'][0]
    assert bal['em_estoque_efetivo'] == 0
    assert linha['projecao'][1]['saldo'] == -50
    assert linha['entregas_risco'][0]['data'] == (hoje() + timedelta(days=3)).isoformat()
    assert linha['entregas_risco'][0]['faltam'] == 50


@pytest.mark.parametrize('origem', ['site', 'b2b'])
def test_demanda_so_iminente_continua_visivel_fora_das_sugestoes_de_loja(app, origem):
    rec = _receita(lead=2)
    rec.sugerir_pedido_loja = False
    _pedido(rec, origem, dias=1)

    bal = balanco_industria(usar_cache=False)['itens'][0]
    crono = cronograma_producao()
    linha = crono['receitas'][0]
    assert bal['comprometido_iminente'] == 50
    assert bal['produzir'] == linha['total'] == 0  # não produz depois da entrega
    assert linha['projecao'][1]['saida_firme'] == 50
    assert linha['entregas_risco'][0]['faltam'] == 50
    assert crono['alertas_falta'][0]['receita_id'] == rec.id


def test_janela_com_offset_e_lead_desconta_demanda_uma_vez(app):
    rec = _receita(lead=2)
    _pedido(rec, 'site', dias=1, qtd=30)
    _pedido(rec, 'site', dias=3, qtd=50)
    db.session.add(EstoqueProducao(receita_id=rec.id, quantidade=80))
    db.session.commit()

    bal = balanco_industria(usar_cache=False, inicio_offset_dias=1)['itens'][0]
    linha = cronograma_producao(inicio_offset_dias=1)['receitas'][0]
    assert bal['em_estoque_efetivo'] == 50
    assert linha['total'] == 0
    assert linha['projecao'][0]['saldo'] == 50
    assert linha['projecao'][2]['saldo'] == 0
    assert linha['entregas_risco'] == []


@pytest.mark.parametrize('offset,lead', [(2, 0), (1, 2)])
def test_wip_parcial_entra_quando_pronto_antes_ou_dentro_da_janela(app, offset, lead):
    rec = _receita(lead=lead)
    _pedido(rec, 'site', dias=3)
    # Das 50 unidades enviadas, 20 já estão no estoque real; só faltam 30.
    plano = PlanejamentoProducao(data=hoje(), origem='cronograma',
                                 enviado_ao_padeiro=True)
    db.session.add(plano)
    db.session.flush()
    db.session.add(PlanejamentoItem(
        planejamento_id=plano.id, receita_id=rec.id, qtd_alvo=50, produzido_qtd=20))
    db.session.add(EstoqueProducao(receita_id=rec.id, quantidade=20))
    db.session.commit()

    bal = balanco_industria(usar_cache=False, inicio_offset_dias=offset)['itens'][0]
    linha = cronograma_producao(inicio_offset_dias=offset)['receitas'][0]
    assert bal['em_producao'] == 30
    assert bal['em_producao_por_dia'] == {(hoje() + timedelta(days=lead)).isoformat(): 30}
    assert linha['total'] == 0
    assert linha['projecao'][3 - offset]['saldo'] == 0
    assert linha['entregas_risco'] == []
    if lead >= offset:
        assert linha['projecao'][0]['saldo'] == 20
        assert linha['projecao'][lead - offset]['producao'] == 30
    else:
        assert linha['projecao'][0]['saldo'] == 50


def test_wip_com_lead_nao_esconde_entrega_anterior_ao_termino(app):
    rec = _receita(lead=2)
    _pedido(rec, 'site', dias=1)
    plano = PlanejamentoProducao(data=hoje(), origem='cronograma',
                                 enviado_ao_padeiro=True)
    db.session.add(plano)
    db.session.flush()
    db.session.add(PlanejamentoItem(
        planejamento_id=plano.id, receita_id=rec.id, qtd_alvo=50, produzido_qtd=20))
    db.session.add(EstoqueProducao(receita_id=rec.id, quantidade=20))
    db.session.commit()

    linha = cronograma_producao(inicio_offset_dias=1)['receitas'][0]
    assert linha['total'] == 0
    assert linha['projecao'][0]['producao'] == 0
    assert linha['projecao'][0]['saldo'] == -30
    assert linha['projecao'][1]['producao'] == 30
    assert linha['entregas_risco'][0]['faltam'] == 30

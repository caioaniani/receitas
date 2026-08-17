"""Flag `Receita.estoque_nao_abate` (dono, 19/07/2026).

Caso real — Massa para folhar: o ledger de EstoqueProducao dizia 2 bolas que
não existiam na geladeira e a sugestão de massa pros 300 pains de terça saía
5 em vez de 7 ("não é só isso de massa que eu preciso"). Decisão do dono:
pra receitas com a flag, o estoque FÍSICO nunca abate a produção sugerida
(balanço + MRP do cronograma); só a produção JÁ MANDADA ao padeiro (WIP)
desconta. O consumo real na produção segue debitando o estoque — a flag é
exclusivamente de planejamento.
"""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import (
    EstoqueProducao,
    Loja,
    PedidoItem,
    PedidoLoja,
    Receita,
    ReceitaIngrediente,
)
from app.utils import hoje


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao so seg-sex (dono 17/08/2026) tornou o shaping do cronograma
    sensivel ao dia da semana — congela hoje() numa SEGUNDA pros cenarios
    hoje()+N deste arquivo cairem sempre em dia util, em qualquer dia em que
    a suite rode (ver conftest.congela_hoje)."""
    congela_hoje()


def _cenario(qtd=100, dias_entrega=3, estoque_massa=0, flag=True):
    """Croissant (lead 0, 1 bola por 50 un) consome 'Massa NA' (lead 1d) —
    espelho do cenário da véspera em test_cronograma_ux, com a flag."""
    loja = Loja(nome='Loja NA', ativa=True)
    massa = Receita(nome='Massa NA', categoria='Paes', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=1000.0,
                    dias_producao=1, estoque_nao_abate=flag)
    cro = Receita(nome='Croissant NA', categoria='Folhados',
                  rendimento_qtd=50, rendimento_unidade='un',
                  peso_base=1000.0)
    db.session.add_all([loja, massa, cro])
    db.session.flush()
    db.session.add(ReceitaIngrediente(
        receita_id=cro.id, tipo='receita', sub_receita_id=massa.id,
        ingrediente_nome='Massa NA', porcentagem=1))
    if estoque_massa:
        db.session.add(EstoqueProducao(receita_id=massa.id,
                                       quantidade=estoque_massa))
    dd = hoje() + timedelta(days=dias_entrega)
    p = PedidoLoja(loja_id=loja.id, status='pendente', data_entrega=dd,
                   data_pedido=dd)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=cro.id,
                              quantidade=qtd))
    db.session.commit()
    return massa, cro


def test_mrp_flag_ignora_estoque_fisico(app):
    """Com a flag, o estoque físico da massa NÃO engole o consumo: a véspera
    agenda as 2 bolas inteiras mesmo com 5 'em estoque' no ledger."""
    from app.services.previsao_producao import cronograma_producao
    massa, cro = _cenario(qtd=100, dias_entrega=3, estoque_massa=5)
    crono = cronograma_producao(horizonte_dias=7)
    rm = next(x for x in crono['receitas'] if x['receita_id'] == massa.id)
    rc = next(x for x in crono['receitas'] if x['receita_id'] == cro.id)
    assert rm['total'] == 2                        # 100 × 1/50, sem desconto
    dia_cro = next(i for i, c in enumerate(rc['por_dia']) if c['qtd'] > 0)
    dia_massa = next(i for i, c in enumerate(rm['por_dia']) if c['qtd'] > 0)
    assert dia_massa == dia_cro - 1                # segue na véspera
    assert rm['estoque_nao_abate'] is True         # tag na linha/sonda
    assert rm['em_estoque'] == 5                   # o número real segue exibido


def test_mrp_sem_flag_estoque_cobre(app):
    """Regressão do comportamento padrão: SEM a flag, o mesmo estoque cobre o
    consumo e a massa não produz nada."""
    from app.services.previsao_producao import cronograma_producao
    massa, _cro = _cenario(qtd=100, dias_entrega=3, estoque_massa=5,
                           flag=False)
    crono = cronograma_producao(horizonte_dias=7)
    rm = next(x for x in crono['receitas'] if x['receita_id'] == massa.id)
    assert rm['total'] == 0
    assert 'estoque_nao_abate' not in rm


def test_flag_vespera_fisico_nao_cobre_vira_aviso(app):
    """Consumo DENTRO do lead (croissant de hoje): com a flag, o físico não
    cobre a véspera — vira aviso 'sem véspera' em vez de sumir em silêncio.
    (É o desejado: se o saldo do sistema não reflete a geladeira, o dono
    precisa ver a falta e conferir na mão.)"""
    from app.services.previsao_producao import cronograma_producao
    massa, _cro = _cenario(qtd=100, dias_entrega=0, estoque_massa=5)
    crono = cronograma_producao(horizonte_dias=7)
    rm = next(x for x in crono['receitas'] if x['receita_id'] == massa.id)
    assert rm['total'] == 0                        # produzir hoje não serve
    sv = rm['insumo_sem_vespera']
    assert sv['faltam'] == 2.0                     # 100 × 1/50, físico fora


def test_flag_wip_plano_de_hoje_conta(app):
    """A produção JÁ MANDADA ao padeiro (plano de hoje) segue descontando:
    croissant de amanhã com 2 bolas no plano de hoje → véspera coberta, sem
    aviso e sem produção nova (não duplica a ordem em execução)."""
    from app.models import PlanejamentoItem, PlanejamentoProducao
    from app.services.previsao_producao import cronograma_producao
    massa, _cro = _cenario(qtd=100, dias_entrega=1, estoque_massa=5)
    plano = PlanejamentoProducao(data=hoje(), origem='cronograma',
                                 status='aprovado', enviado_ao_padeiro=True)
    db.session.add(plano)
    db.session.flush()
    db.session.add(PlanejamentoItem(planejamento_id=plano.id,
                                    receita_id=massa.id, multiplicador=1,
                                    qtd_alvo=2, produzido_qtd=0))
    db.session.commit()
    crono = cronograma_producao(horizonte_dias=6, inicio_offset_dias=1)
    rm = next(x for x in crono['receitas'] if x['receita_id'] == massa.id)
    assert rm['total'] == 0                        # WIP cobre o consumo
    assert 'insumo_sem_vespera' not in rm


def test_balanco_flag_ignora_fisico(app):
    """No balanço, receita VENDIDA com a flag produz a demanda inteira mesmo
    com estoque físico registrado (o em_estoque_efetivo exibido segue real)."""
    from app.services.previsao_producao import balanco_industria
    loja = Loja(nome='Loja NA Bal', ativa=True)
    r = Receita(nome='Pao NA Bal', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0,
                estoque_nao_abate=True)
    db.session.add_all([loja, r])
    db.session.flush()
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=10))
    dd = hoje() + timedelta(days=2)
    p = PedidoLoja(loja_id=loja.id, status='pendente', data_entrega=dd,
                   data_pedido=dd)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=8))
    db.session.commit()
    bal = balanco_industria(usar_cache=False)
    it = next(x for x in bal['itens'] if x['receita_id'] == r.id)
    assert it['produzir'] == 8                     # físico (10) ignorado
    assert it['em_estoque_efetivo'] == 10          # o real segue reportado
    assert it['estoque_nao_abate'] is True


def test_balanco_sem_flag_estoque_abate(app):
    """Regressão: sem a flag os mesmos 10 em estoque cobrem os 8 pedidos."""
    from app.services.previsao_producao import balanco_industria
    loja = Loja(nome='Loja NA Bal2', ativa=True)
    r = Receita(nome='Pao NA Bal2', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0)
    db.session.add_all([loja, r])
    db.session.flush()
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=10))
    dd = hoje() + timedelta(days=2)
    p = PedidoLoja(loja_id=loja.id, status='pendente', data_entrega=dd,
                   data_pedido=dd)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=8))
    db.session.commit()
    bal = balanco_industria(usar_cache=False)
    it = next(x for x in bal['itens'] if x['receita_id'] == r.id)
    assert it['produzir'] == 0
    assert it['estoque_nao_abate'] is False


def test_cronograma_linha_vendida_coerente_com_a_flag(app):
    """Receita VENDIDA com a flag: a linha do grid, o saldo do expandir e o
    alerta de risco contam a MESMA história do balanço — o físico fantasma
    não faz a caixa dizer "não falta" enquanto a linha produz (classe do bug
    de 30/06), nem cala o 🚨 de entrega em risco."""
    from app.services.previsao_producao import cronograma_producao
    loja = Loja(nome='Loja NA Grid', ativa=True)
    r = Receita(nome='Pao NA Grid', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0,
                estoque_nao_abate=True)
    db.session.add_all([loja, r])
    db.session.flush()
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=10))
    dd = hoje() + timedelta(days=2)
    p = PedidoLoja(loja_id=loja.id, status='pendente', data_entrega=dd,
                   data_pedido=dd)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=8))
    db.session.commit()
    crono = cronograma_producao(horizonte_dias=7)
    rr = next(x for x in crono['receitas'] if x['receita_id'] == r.id)
    assert rr['total'] == 8                        # shaping distribui os 8
    assert sum(c['qtd'] for c in rr['por_dia']) == 8
    assert rr['produzir'] == 8                     # linha = balanço
    assert rr['saldo'] == -8                       # caixa não diz "não falta"
    assert rr['em_estoque'] == 10                  # o real segue visível
    # Produção programada cobre a entrega → sem falso 🚨.
    assert rr['entregas_risco'] == []


def test_flag_fantasma_nao_cala_alerta_de_risco(app):
    """Entrega firme que a produção NÃO alcança (lead longo): sem a flag o
    fantasma de 10 'cobriria' e o 🚨 ficava mudo; com a flag o alerta sai."""
    from app.services.previsao_producao import cronograma_producao
    loja = Loja(nome='Loja NA Risco', ativa=True)
    r = Receita(nome='Pao NA Risco', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0,
                dias_producao=2, estoque_nao_abate=True)
    db.session.add_all([loja, r])
    db.session.flush()
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=10))
    dd = hoje() + timedelta(days=1)               # amanhã: lead 2 não alcança
    p = PedidoLoja(loja_id=loja.id, status='pendente', data_entrega=dd,
                   data_pedido=dd)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=8))
    db.session.commit()
    crono = cronograma_producao(horizonte_dias=7)
    rr = next(x for x in crono['receitas'] if x['receita_id'] == r.id)
    assert rr['entregas_risco'], 'fantasma não pode calar o alerta'
    assert rr['entregas_risco'][0]['faltam'] == 8


def test_ficha_salva_a_flag(app, admin_user):
    r = Receita(nome='Massa Flag UI', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0)
    db.session.add(r)
    db.session.commit()
    rid = r.id
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    resp = c.post(f'/receitas/{rid}/salvar', data={
        'nome': 'Massa Flag UI', 'categoria': 'Paes',
        'rendimento_qtd': '1', 'rendimento_unidade': 'un',
        'peso_base': '1000', 'estoque_nao_abate': '1',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert db.session.get(Receita, rid).estoque_nao_abate is True
    # Desmarcar (form sem o campo) desliga.
    resp = c.post(f'/receitas/{rid}/salvar', data={
        'nome': 'Massa Flag UI', 'categoria': 'Paes',
        'rendimento_qtd': '1', 'rendimento_unidade': 'un',
        'peso_base': '1000',
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert db.session.get(Receita, rid).estoque_nao_abate is False

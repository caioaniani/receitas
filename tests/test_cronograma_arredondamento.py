"""Arredondamento previsão↔distribuição (TOP 1, 30/06/2026).

Bug de giro baixo: o previsto é fração/dia (ex: 0,43). Ele era arredondado por
dia (round->0) em alguns caminhos e somado-então-ceil em outros, gerando:
- a página /previsao dizia "total previsto 0" enquanto o grid mandava produzir 3
  (a ferramenta de diagnóstico desmentia o plano);
- a produção colapsava toda no dia 0 (pesos todos zero -> dump no 1º dia).

Estes testes travam: total do diagnóstico == previsto do balanço (ceil da soma
fracionária), e a distribuição espalha em vez de empilhar no dia 0.
"""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import Loja, PedidoItem, PedidoLoja, Receita
from app.services.previsao_producao import (
    balanco_industria,
    cronograma_producao,
    decompor_previsao,
)
from app.utils import hoje


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao seg-sex + janela semanal tornaram o motor weekday-sensivel
    — congela numa SEGUNDA fixa (mesma fixture dos arquivos do cronograma;
    caso real 19/08/2026: test_cronograma_edit quebrou na QUARTA porque o
    indice 3 do grid caiu no sabado bloqueado)."""
    congela_hoje()



def _setup_giro_baixo():
    """6 entregas em 6 dias-da-semana DISTINTOS na janela (cada dow tem 1
    ocorrência < 2 -> cai no fallback de média diária), qtd 3 cada -> soma 18
    em 42 dias = 0,4286/dia. Sem estoque e sem pedido firme em aberto."""
    r = Receita(nome='Croissant Giro Baixo', categoria='Croissants',
                rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0)
    loja = Loja(nome='Loja Giro', ativa=True)
    db.session.add_all([r, loja])
    db.session.commit()
    hoje_d = hoje()
    for dias_atras in (1, 2, 3, 4, 5, 6):   # 6 dias-da-semana distintos
        p = PedidoLoja(loja_id=loja.id, status='recebido',
                       data_entrega=hoje_d - timedelta(days=dias_atras),
                       data_pedido=hoje_d - timedelta(days=dias_atras + 1))
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=3))
    db.session.commit()
    return r


def test_diagnostico_bate_com_o_grid_em_giro_baixo(app):
    """B1: /previsao (decompor) e o balanço usam a MESMA conta (ceil da soma
    fracionária). Antes: grid 'produzir 3' vs diagnóstico 'total previsto 0'."""
    with app.app_context():
        r = _setup_giro_baixo()
        bal = balanco_industria(usar_cache=False)
        it = next(i for i in bal['itens'] if i['receita_id'] == r.id)
        dec = decompor_previsao(r.id)
    assert it['previsto'] > 0                          # previsto real, não 0
    assert dec['total_previsto'] == it['previsto']     # diagnóstico == plano


def test_diagnostico_previsto_por_dia_e_fracionario(app):
    """B1: cada dia mostra a fração honesta (ex: 0,4), não round->0; e a soma
    das frações fecha no total (ceil)."""
    with app.app_context():
        r = _setup_giro_baixo()
        dec = decompor_previsao(r.id)
    # algum dia tem previsto fracionário > 0 (antes era tudo 0 por arredondar)
    assert any(0 < d['previsto'] < 1 for d in dec['dias'])
    from math import ceil
    assert dec['total_previsto'] == ceil(sum(d['previsto'] for d in dec['dias']))


def test_caixa_saldo_reflete_o_previsto(app):
    """B3: a caixa de saldo considera o previsto, não só o firme. Antes:
    saldo = estoque − comprometido (só firme) -> dizia '✓ não falta' enquanto a
    linha mandava produzir. Agora saldo = estoque_útil − max(firme, previsto),
    e -saldo == produzir (a caixa bate com a linha)."""
    with app.app_context():
        r = _setup_giro_baixo()
        crono = cronograma_producao()
        rr = next(x for x in crono['receitas'] if x['receita_id'] == r.id)
    assert rr['comprometido'] == 0                 # nenhum pedido firme em aberto
    assert rr['previsto'] > 0                       # mas há demanda prevista
    assert rr['demanda'] == max(rr['comprometido'], rr['previsto'])
    assert rr['produzir'] > 0                       # então precisa produzir
    assert rr['saldo'] == rr['em_estoque_efetivo'] - rr['demanda']
    assert rr['saldo'] < 0                          # antes dava 0 ("não falta")
    assert rr['produzir'] == -rr['saldo']           # caixa == linha


def test_producao_nao_colapsa_no_dia_zero(app):
    """B2: produção de giro baixo espalha pelos dias (pesos fracionários), em
    vez de empilhar tudo no dia 0."""
    with app.app_context():
        r = _setup_giro_baixo()
        crono = cronograma_producao()
        rr = next(x for x in crono['receitas'] if x['receita_id'] == r.id)
    qtds = [c['qtd'] for c in rr['por_dia']]
    assert sum(qtds) == rr['total'] > 0            # conserva o total
    assert sum(1 for q in qtds if q > 0) >= 2      # espalhou (>= 2 dias com produção)
    assert qtds[0] < sum(qtds)                     # dia 0 não leva tudo

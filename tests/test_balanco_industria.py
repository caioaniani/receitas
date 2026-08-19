"""Testes do balanco de producao da industria (app.services.previsao_producao).

Cobre os pontos de risco da regra de estoque:
- comprometido conta so pedido AINDA NAO enviado (em_transporte ja baixou).
- so Receita entra (produto/MP de fora — producao = ficha tecnica).
- produzir = max(0, max(comprometido, previsto) - estoque).
- previsao por dia-da-semana sobre historico de PedidoLoja.
- cancelado nao conta (nem comprometido nem historico).

Todas as chamadas usam usar_cache=False — o cache e in-memory por processo e
sobrevive ao reset de banco entre testes.
"""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import (
    EstoqueProducao,
    Loja,
    MateriaPrima,
    PedidoItem,
    PedidoLoja,
    Produto,
    Receita,
)
from app.services.previsao_producao import balanco_industria
from app.utils import hoje


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao seg-sex + janela semanal tornaram o motor weekday-sensivel
    — congela numa SEGUNDA fixa (mesma fixture dos arquivos do cronograma;
    caso real 19/08/2026: test_cronograma_edit quebrou na QUARTA porque o
    indice 3 do grid caiu no sabado bloqueado)."""
    congela_hoje()



def _receita(nome='Croissant'):
    r = Receita(nome=nome, categoria='Croissants', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def _loja(nome='Loja A'):
    loja = Loja(nome=nome, ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


def _pedido(loja, status, data_entrega, itens):
    """itens = lista de (receita_id, produto_id, mp_id, qtd)."""
    p = PedidoLoja(loja_id=loja.id, status=status, data_entrega=data_entrega,
                   data_pedido=data_entrega)
    db.session.add(p)
    db.session.flush()
    for rid, pid, mid, qtd in itens:
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=rid,
                                  produto_id=pid, materia_prima_id=mid,
                                  quantidade=qtd))
    db.session.commit()
    return p


def _por_receita(res, receita_id):
    for it in res['itens']:
        if it['receita_id'] == receita_id:
            return it
    return None


def test_comprometido_ignora_pedido_ja_enviado(app):
    """em_transporte ja baixou o EstoqueProducao — nao pode contar de novo."""
    loja = _loja()
    r = _receita()
    d = hoje() + timedelta(days=1)
    _pedido(loja, 'pendente', d, [(r.id, None, None, 40)])
    _pedido(loja, 'em_transporte', d, [(r.id, None, None, 100)])

    res = balanco_industria(horizonte_dias=7, usar_cache=False)
    it = _por_receita(res, r.id)
    assert it is not None
    assert it['comprometido'] == 40   # so o pendente
    assert it['produzir'] == 40       # sem estoque/historico


def test_so_receita_nao_produto_nem_mp(app):
    """Producao = ficha tecnica = Receita. Produto/MP nao entram no balanco."""
    loja = _loja()
    r = _receita()
    p = Produto(nome='Refrigerante', ativo=True)
    mp = MateriaPrima(nome='Farinha', unidade='kg', custo_por_kg=5.0)
    db.session.add_all([p, mp])
    db.session.commit()
    d = hoje() + timedelta(days=1)
    _pedido(loja, 'pendente', d, [
        (r.id, None, None, 5),
        (None, p.id, None, 99),
        (None, None, mp.id, 99),
    ])

    res = balanco_industria(horizonte_dias=7, usar_cache=False)
    assert len(res['itens']) == 1            # so a receita
    assert _por_receita(res, r.id)['comprometido'] == 5


def test_estoque_cobre_demanda(app):
    loja = _loja()
    r = _receita()
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=50))
    db.session.commit()
    d = hoje() + timedelta(days=1)
    _pedido(loja, 'pendente', d, [(r.id, None, None, 30)])

    res = balanco_industria(horizonte_dias=7, usar_cache=False)
    it = _por_receita(res, r.id)
    assert it['em_estoque'] == 50
    assert it['comprometido'] == 30
    assert it['produzir'] == 0          # 50 cobre os 30


def test_previsao_por_dia_da_semana(app):
    """3 ocorrencias do mesmo dia-da-semana -> media daquele dia."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    for semanas in (1, 2, 3):
        _pedido(loja, 'recebido', hoje_d - timedelta(days=7 * semanas),
                [(r.id, None, None, 10)])

    # Horizonte de 1 dia = so hoje (mesmo dia-da-semana das ocorrencias).
    res = balanco_industria(horizonte_dias=1, janela_semanas=6,
                            usar_cache=False)
    it = _por_receita(res, r.id)
    assert it is not None
    assert it['previsto'] == 10
    assert it['comprometido'] == 0
    assert it['produzir'] == 10
    assert it['tem_historico'] is True
    assert res['profundidade']['n_pedidos'] == 3


def test_produzir_usa_previsto_quando_maior(app):
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    for semanas in (1, 2, 3):
        _pedido(loja, 'recebido', hoje_d - timedelta(days=7 * semanas),
                [(r.id, None, None, 50)])
    _pedido(loja, 'pendente', hoje_d, [(r.id, None, None, 8)])

    res = balanco_industria(horizonte_dias=1, janela_semanas=6,
                            usar_cache=False)
    it = _por_receita(res, r.id)
    assert it['comprometido'] == 8
    assert it['previsto'] == 50
    assert it['produzir'] == 50         # max(8, 50)


def test_produzir_usa_comprometido_quando_maior(app):
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    for semanas in (1, 2, 3):
        _pedido(loja, 'recebido', hoje_d - timedelta(days=7 * semanas),
                [(r.id, None, None, 5)])
    _pedido(loja, 'pendente', hoje_d, [(r.id, None, None, 40)])

    res = balanco_industria(horizonte_dias=1, janela_semanas=6,
                            usar_cache=False)
    it = _por_receita(res, r.id)
    assert it['previsto'] == 5
    assert it['comprometido'] == 40
    assert it['produzir'] == 40         # max(40, 5)


def test_cancelado_nao_conta(app):
    """Cancelado nao e demanda real — fora do comprometido e do historico."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    _pedido(loja, 'cancelado', hoje_d - timedelta(days=7),
            [(r.id, None, None, 10)])
    _pedido(loja, 'cancelado', hoje_d + timedelta(days=1),
            [(r.id, None, None, 10)])

    res = balanco_industria(horizonte_dias=7, usar_cache=False)
    assert _por_receita(res, r.id) is None     # sem sinal -> nao aparece
    assert res['profundidade']['n_pedidos'] == 0


def test_receita_arquivada_fora(app):
    """Receita arquivada nao e produzida — fica fora do balanco."""
    from app.utils import agora
    loja = _loja()
    r = _receita()
    r.arquivada_em = agora()
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1),
            [(r.id, None, None, 20)])

    res = balanco_industria(horizonte_dias=7, usar_cache=False)
    assert _por_receita(res, r.id) is None


def test_lead_time_desloca_comprometido(app):
    """dias_producao=2 (pao de 48h): o comprometido de hoje olha as entregas
    de daqui 2 dias, nao as de hoje (tarde demais pra produzir com 48h)."""
    loja = _loja()
    r = _receita()
    r.dias_producao = 2
    db.session.commit()
    hoje_d = hoje()
    _pedido(loja, 'pendente', hoje_d + timedelta(days=2),
            [(r.id, None, None, 30)])
    _pedido(loja, 'pendente', hoje_d, [(r.id, None, None, 99)])

    res = balanco_industria(horizonte_dias=1, usar_cache=False)
    it = _por_receita(res, r.id)
    assert it is not None
    assert it['comprometido'] == 30     # entrega hoje+2; ignora a de hoje
    assert it['dias_producao'] == 2
    assert it['produzir'] == 30


def test_lead_time_zero_inalterado(app):
    """Sem lead (default 0): janela = horizonte a partir de hoje (como antes)."""
    loja = _loja()
    r = _receita()
    _pedido(loja, 'pendente', hoje(), [(r.id, None, None, 40)])

    res = balanco_industria(horizonte_dias=1, usar_cache=False)
    it = _por_receita(res, r.id)
    assert it['comprometido'] == 40
    assert it['dias_producao'] == 0


def test_lead_time_previsto_usa_dow_do_dia_alvo(app):
    """Com lead=2, a previsao usa o dia-da-semana de (hoje+2), nao de hoje."""
    loja = _loja()
    r = _receita()
    r.dias_producao = 2
    db.session.commit()
    alvo = hoje() + timedelta(days=2)
    for semanas in (1, 2, 3):
        _pedido(loja, 'recebido', alvo - timedelta(days=7 * semanas),
                [(r.id, None, None, 10)])

    res = balanco_industria(horizonte_dias=1, janela_semanas=6,
                            usar_cache=False)
    it = _por_receita(res, r.id)
    assert it['previsto'] == 10        # media do dow de hoje+2 (3 ocorrencias)


def test_breakdown_lista_todas_lojas_operacionais(app):
    """O breakdown_comprometido lista TODAS as lojas operacionais (ativas,
    sem Industria), inclusive as que NAO pediram a receita (qtd=0). Sem isso,
    o usuario ve so a loja que pediu e pensa que o motor filtrou as demais.

    Tambem trava: Industria NAO aparece no breakdown (loja de servico
    interna, nao operacional); loja inativa tambem NAO aparece."""
    loja_a = _loja('Loja A')
    loja_b = _loja('Loja B')
    loja_inativa = Loja(nome='Loja Inativa', ativa=False)
    industria = Loja(nome='Industria', ativa=True)
    db.session.add_all([loja_inativa, industria])
    db.session.commit()

    r = _receita()
    # So Loja A pede a receita.
    _pedido(loja_a, 'pendente', hoje() + timedelta(days=1),
            [(r.id, None, None, 50)])

    res = balanco_industria(horizonte_dias=7, usar_cache=False)
    it = _por_receita(res, r.id)
    assert it is not None

    breakdown = it['breakdown_comprometido']
    nomes = [b['loja_nome'] for b in breakdown]
    qtds_por_nome = {b['loja_nome']: b['qtd'] for b in breakdown}

    assert 'Loja A' in nomes
    assert 'Loja B' in nomes        # zerada mas listada (a virada da UX)
    assert 'Industria' not in nomes
    assert 'Loja Inativa' not in nomes

    assert qtds_por_nome['Loja A'] == 50
    assert qtds_por_nome['Loja B'] == 0

    # Ordem: qtd desc, depois alfabetica entre zeradas.
    assert breakdown[0]['loja_nome'] == 'Loja A'    # qtd 50 vem primeiro

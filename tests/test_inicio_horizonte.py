"""Início do horizonte de planejamento (offset). O painel/grade/pedidos passam
a começar AMANHÃ por padrão (a produção de hoje já está decidida), com seletor
"A partir de" pra escolher Hoje / Amanhã / +N dias.

Funções: inicio_offset_dias desloca SÓ a janela futura; o histórico continua
ancorado em hoje. Default 0 nas funções (retrocompatível); as ROTAS default 1.
"""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import EstoqueProducao, Loja, PedidoItem, PedidoLoja, Receita
from app.services.previsao_producao import (
    balanco_industria,
    cronograma_producao,
    grade_loja_dia,
    sugerir_pedidos_semana,
)
from app.utils import hoje


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao seg-sex + janela semanal tornaram o motor weekday-sensivel
    — congela numa SEGUNDA fixa (mesma fixture dos arquivos do cronograma;
    caso real 19/08/2026: test_cronograma_edit quebrou na QUARTA porque o
    indice 3 do grid caiu no sabado bloqueado)."""
    congela_hoje()



def _loja(nome='Loja A'):
    loja = Loja(nome=nome, ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


def _receita(nome='Moeda', dias_producao=0):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0,
                dias_producao=dias_producao)
    db.session.add(r)
    db.session.commit()
    return r


def _pedido(loja, status, data_entrega, receita, qtd):
    p = PedidoLoja(loja_id=loja.id, status=status, data_entrega=data_entrega,
                   data_pedido=data_entrega)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=receita.id,
                              quantidade=qtd))
    db.session.commit()
    return p


def _linha(grade, loja_id):
    for entry in grade['lojas']:
        if entry['loja_id'] == loja_id:
            return entry
    return None


# ── grade_loja_dia ──────────────────────────────────────────────────────────

def test_grade_offset_0_inclui_hoje(app):
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    _pedido(loja, 'pendente', hoje_d, r, 10)
    _pedido(loja, 'pendente', hoje_d + timedelta(days=1), r, 20)

    g = grade_loja_dia(r.id, horizonte_dias=7, inicio_offset_dias=0)
    assert g['dias'][0]['data'] == hoje_d.isoformat()
    assert _linha(g, loja.id)['celulas'][0]['firme'] == 10
    assert g['total_firme'] == 30


def test_grade_offset_1_comeca_amanha_exclui_hoje(app):
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    _pedido(loja, 'pendente', hoje_d, r, 10)            # hoje -> fora
    _pedido(loja, 'pendente', hoje_d + timedelta(days=1), r, 20)   # amanhã

    g = grade_loja_dia(r.id, horizonte_dias=7, inicio_offset_dias=1)
    assert g['dias'][0]['data'] == (hoje_d + timedelta(days=1)).isoformat()
    assert g['inicio'] == (hoje_d + timedelta(days=1)).isoformat()
    # o pedido de hoje não conta; só o de amanhã
    assert g['total_firme'] == 20
    assert _linha(g, loja.id)['celulas'][0]['firme'] == 20


# ── balanco_industria ───────────────────────────────────────────────────────

def test_balanco_offset_1_exclui_comprometido_de_hoje(app):
    loja = _loja()
    r = _receita(dias_producao=0)
    hoje_d = hoje()
    _pedido(loja, 'pendente', hoje_d, r, 10)
    _pedido(loja, 'pendente', hoje_d + timedelta(days=1), r, 20)

    b0 = balanco_industria(horizonte_dias=7, inicio_offset_dias=0,
                           usar_cache=False)
    it0 = next(i for i in b0['itens'] if i['receita_id'] == r.id)
    assert it0['comprometido'] == 30      # hoje + amanhã

    b1 = balanco_industria(horizonte_dias=7, inicio_offset_dias=1,
                           usar_cache=False)
    it1 = next(i for i in b1['itens'] if i['receita_id'] == r.id)
    assert it1['comprometido'] == 20      # só amanhã
    assert b1['inicio'] == (hoje_d + timedelta(days=1)).isoformat()


def test_balanco_cache_separa_por_offset(app):
    """Offsets diferentes não podem compartilhar entrada de cache."""
    loja = _loja()
    r = _receita()
    hoje_d = hoje()
    _pedido(loja, 'pendente', hoje_d, r, 10)
    _pedido(loja, 'pendente', hoje_d + timedelta(days=1), r, 20)

    b0 = balanco_industria(horizonte_dias=7, inicio_offset_dias=0)
    b1 = balanco_industria(horizonte_dias=7, inicio_offset_dias=1)
    c0 = next(i for i in b0['itens'] if i['receita_id'] == r.id)['comprometido']
    c1 = next(i for i in b1['itens'] if i['receita_id'] == r.id)['comprometido']
    assert c0 == 30 and c1 == 20


# ── sugerir_pedidos_semana ──────────────────────────────────────────────────

def test_sugerir_offset_1_comeca_amanha(app):
    _loja()
    s = sugerir_pedidos_semana(horizonte_dias=7, inicio_offset_dias=1)
    hoje_d = hoje()
    assert s['dias'][0]['data'] == (hoje_d + timedelta(days=1)).isoformat()
    assert s['inicio'] == (hoje_d + timedelta(days=1)).isoformat()


# ── rotas: default = amanhã, seletor respeitado ─────────────────────────────

def _login(client, admin_user):
    client.post('/auth/login',
                data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)


def test_rota_painel_default_amanha(app, admin_user):
    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/producao/painel')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'name="inicio"' in body
    assert 'value="1" selected' in body      # Amanhã selecionado por padrão
    assert 'a partir de' in body


def test_rota_painel_inicio_hoje(app, admin_user):
    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/producao/painel?inicio=0')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'value="0" selected' in body       # Hoje selecionado


# ── estoque: entrega iminente consome estoque (não subproduzir) ──────────────

def test_entrega_iminente_consome_estoque_nao_subproduz(app):
    """REGRESSÃO (estoque): uma entrega IMINENTE (entre hoje e o início da
    janela) consome estoque e não pode mais ser produzida neste horizonte. O
    estoque efetivo desconta essa demanda — senão o balanço acharia o estoque
    livre pra a janela e SUBPRODUZIRIA (risco de ruptura)."""
    loja = _loja()
    r = _receita(dias_producao=1)
    hoje_d = hoje()
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=50))
    db.session.commit()
    # Início = amanhã (offset 1). A entrega de amanhã (lead 1 -> produziria
    # hoje, fora da janela) é iminente e come 40 do estoque.
    _pedido(loja, 'pendente', hoje_d + timedelta(days=1), r, 40)   # iminente
    _pedido(loja, 'pendente', hoje_d + timedelta(days=2), r, 30)   # na janela

    bal = balanco_industria(horizonte_dias=7, inicio_offset_dias=1,
                            usar_cache=False)
    it = next(i for i in bal['itens'] if i['receita_id'] == r.id)
    assert it['em_estoque'] == 50            # estoque físico inalterado
    assert it['em_estoque_efetivo'] == 10    # 50 - 40 da entrega iminente
    assert it['produzir'] == 20              # janela 30 - 10 efetivo (não 0!)

    # cronograma distribui o mesmo total, sem front-loadar o estoque já-consumido
    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=1)
    rr = next(x for x in crono['receitas'] if x['receita_id'] == r.id)
    assert rr['total'] == 20
    assert sum(c['qtd'] for c in rr['por_dia']) == 20

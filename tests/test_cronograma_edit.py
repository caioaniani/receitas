"""Edição manual das células do cronograma (29/06/2026, modelo por-célula 30/06).

Edição POR CÉLULA: cada dia editado salva o seu CronogramaOverride; os outros
seguem a sugestão. O total da linha = soma das células — dá pra produzir MAIS
que o sugerido e editar linha zerada. Salva rascunho que o cronograma/aprovar
passam a usar.
"""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import CronogramaOverride, Loja, PedidoItem, PedidoLoja, Receita, ReceitaIngrediente
from app.services.cronograma_edit import editar_celula, resetar_receita
from app.services.previsao_producao import cronograma_producao
from app.utils import hoje


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao seg-sex + janela semanal tornaram o motor weekday-sensivel
    — congela numa SEGUNDA fixa (mesma fixture dos arquivos do cronograma;
    caso real 19/08/2026: test_cronograma_edit quebrou na QUARTA porque o
    indice 3 do grid caiu no sabado bloqueado)."""
    congela_hoje()



def _loja():
    loja = Loja(nome='Loja A', ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


def _receita_amass(nome='Sourdough', rend=50, peso_base=5000, cap=500):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=rend,
                rendimento_unidade='un', peso_base=float(peso_base),
                capacidade_amassadeira_g=cap)
    db.session.add(r)
    db.session.flush()
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                      ingrediente_nome='Farinha', porcentagem=100))
    db.session.commit()
    return r


def _pedido(loja, r, dias, qtd):
    d = hoje() + timedelta(days=dias)
    p = PedidoLoja(loja_id=loja.id, status='pendente', data_entrega=d,
                   data_pedido=d)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=qtd))
    db.session.commit()


def _row(crono, rid):
    return next((x for x in crono['receitas'] if x['receita_id'] == rid), None)


# ── editar_celula: por celula, persiste, cronograma reflete ────────────────
def test_editar_celula_persiste_e_cronograma_reflete(app):
    loja = _loja()
    r = _receita_amass()
    _pedido(loja, r, 2, 30)
    _pedido(loja, r, 4, 30)

    base = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr0 = _row(base, r.id)
    assert rr0['total'] == 60

    # edita SO o dia 0 — salva 1 override, os outros dias seguem a sugestao.
    res = editar_celula(r.id, rr0['por_dia'][0]['data'], 25,
                        horizonte_dias=7, inicio_offset_dias=0)
    assert res is not None
    assert res['por_dia'][0]['qtd'] == 25
    assert CronogramaOverride.query.filter_by(receita_id=r.id).count() == 1

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr = _row(crono, r.id)
    assert rr['por_dia'][0]['qtd'] == 25            # celula editada aplicada
    assert rr.get('editado') is True
    assert rr['total'] == sum(c['qtd'] for c in rr['por_dia'])   # total = soma


def test_editar_celula_pode_produzir_mais_que_o_sugerido(app):
    """E1: editar uma celula PRA CIMA aumenta o total (antes clampava no
    'Produzir' e voltava — 'a edicao nao pegava')."""
    loja = _loja()
    r = _receita_amass()
    _pedido(loja, r, 2, 30)
    base = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr0 = _row(base, r.id)
    total0 = rr0['total']
    res = editar_celula(r.id, rr0['por_dia'][0]['data'], total0 + 50,
                        horizonte_dias=7, inicio_offset_dias=0)
    assert res['por_dia'][0]['qtd'] == total0 + 50       # ficou no valor digitado
    assert res['total'] > total0                          # total cresceu


def test_editar_linha_zerada_programa_producao(app):
    """E2: receita SEM pedido (linha zerada) pode ser programada — editar uma
    celula da 0 pra 40 grava 40 (antes o clamp em [0,0] engolia o numero)."""
    _loja()
    r = _receita_amass(nome='Pain au Chocolat')   # sem pedido nenhum
    base = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr0 = _row(base, r.id)
    assert rr0 is not None and rr0['total'] == 0          # linha zerada existe
    res = editar_celula(r.id, rr0['por_dia'][3]['data'], 40,
                        horizonte_dias=7, inicio_offset_dias=0)
    assert res['por_dia'][3]['qtd'] == 40
    assert res['total'] == 40
    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr = _row(crono, r.id)
    assert rr['por_dia'][3]['qtd'] == 40 and rr['editado'] is True


def test_override_por_celula_aplica_e_total_segue(app):
    """Override de UM dia (ex: vindo da tela 'editar plano') aplica POR CELULA:
    aquele dia mostra o valor manual, os demais seguem a sugestao, e o total da
    linha passa a ser a SOMA das celulas exibidas (nao exige cobrir o horizonte
    inteiro nem somar o total antigo)."""
    from datetime import date

    loja = _loja()
    r = _receita_amass()
    _pedido(loja, r, 2, 30)

    base = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr0 = _row(base, r.id)
    alvo = date.fromisoformat(rr0['por_dia'][0]['data'])
    resto = sum(c['qtd'] for c in rr0['por_dia'][1:])   # sugestao dos outros dias
    db.session.add(CronogramaOverride(receita_id=r.id, data=alvo, qtd=99))
    db.session.commit()

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr = _row(crono, r.id)
    assert rr['editado'] is True
    assert rr['por_dia'][0]['qtd'] == 99           # celula aplicada
    assert rr['total'] == 99 + resto               # total = soma das celulas


def test_resetar_volta_pra_sugestao(app):
    loja = _loja()
    r = _receita_amass()
    _pedido(loja, r, 2, 30)
    base = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr0 = _row(base, r.id)
    editar_celula(r.id, rr0['por_dia'][0]['data'], rr0['total'],
                  horizonte_dias=7, inicio_offset_dias=0)
    assert CronogramaOverride.query.filter_by(receita_id=r.id).count() > 0

    datas = [c['data'] for c in rr0['por_dia']]
    n, _preservados = resetar_receita(r.id, datas)
    assert n == 1                                  # so a celula editada tinha override
    assert CronogramaOverride.query.filter_by(receita_id=r.id).count() == 0
    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    assert not _row(crono, r.id).get('editado')


# ── rota de autosave ───────────────────────────────────────────────────────
def test_rota_celula_salva(app, admin_user):
    loja = _loja()
    r = _receita_amass()
    _pedido(loja, r, 2, 30)
    base = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr0 = _row(base, r.id)

    c = app.test_client()
    c.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
           follow_redirects=True)
    resp = c.post('/telaindustriateste/celula', json={
        'receita_id': r.id, 'data': rr0['por_dia'][0]['data'],
        'qtd': rr0['total'], 'horizonte': 7, 'janela': 6, 'inicio': 0})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['ok'] is True
    assert body['por_dia'][0]['qtd'] == rr0['total']
    assert CronogramaOverride.query.filter_by(receita_id=r.id).count() == 1


def test_rota_limpar_edicoes(app, admin_user):
    """Botão 'limpar edições manuais' apaga TODOS os overrides (volta pro
    cálculo) sem tocar em pedido/estoque."""
    loja = _loja()
    r = _receita_amass()
    _pedido(loja, r, 2, 30)
    base = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr0 = _row(base, r.id)
    editar_celula(r.id, rr0['por_dia'][0]['data'], rr0['total'],
                  horizonte_dias=7, inicio_offset_dias=0)
    assert CronogramaOverride.query.count() > 0

    c = app.test_client()
    c.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
           follow_redirects=True)
    resp = c.post('/telaindustriateste/limpar-edicoes',
                  data={'horizonte': 7, 'janela': 6, 'inicio': 0})
    assert resp.status_code in (302, 303)
    assert CronogramaOverride.query.count() == 0


# ── E3: aviso de edição manual desatualizada ──────────────────────────────────
def _override_antigo(receita_id, data, qtd, dias_atras=2):
    """Cria um override com criado_em no passado (simula edição de dias atrás)."""
    from datetime import datetime

    d = hoje() - timedelta(days=dias_atras)
    o = CronogramaOverride(receita_id=receita_id, data=data, qtd=qtd,
                           criado_em=datetime(d.year, d.month, d.day, 12, 0))
    db.session.add(o)
    db.session.commit()
    return o


def test_override_antigo_divergente_marca_stale(app):
    """E3: edição de um dia anterior que já não bate com o cálculo atual é
    marcada como possivelmente desatualizada, expondo o sugerido e a data."""
    from datetime import date

    loja = _loja()
    r = _receita_amass()
    _pedido(loja, r, 2, 30)                              # calculo sugere 30
    base = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    sugerido = _row(base, r.id)['total']
    alvo = date.fromisoformat(base['receitas'][0]['por_dia'][0]['data'])
    ontem = (hoje() - timedelta(days=2)).isoformat()
    _override_antigo(r.id, alvo, 99, dias_atras=2)       # fixou 99 num dia -> diverge

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr = _row(crono, r.id)
    assert rr['editado'] is True
    assert rr['override_stale'] is True
    assert rr['override_sugerido'] == sugerido           # o que o cálculo diz agora
    assert rr['override_desde'] == ontem                 # data da edição
    assert rr['total'] != sugerido                       # o manual diverge


def test_override_fresco_nao_marca_stale(app):
    """E3: edição feita HOJE (mesmo divergindo) é intencional, não 'stale'."""
    from datetime import date

    loja = _loja()
    r = _receita_amass()
    _pedido(loja, r, 2, 30)
    base = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    alvo = date.fromisoformat(base['receitas'][0]['por_dia'][0]['data'])
    db.session.add(CronogramaOverride(receita_id=r.id, data=alvo, qtd=99))  # criado_em=hoje
    db.session.commit()

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr = _row(crono, r.id)
    assert rr['editado'] is True
    assert rr.get('override_stale') is not True          # fresco -> sem aviso


def test_override_antigo_alinhado_nao_marca_stale(app):
    """E3: edição antiga que ainda BATE com o cálculo não é 'stale' (nada mudou)."""
    from datetime import date

    loja = _loja()
    r = _receita_amass()
    _pedido(loja, r, 2, 30)
    base = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr0 = _row(base, r.id)
    dia_sug = next(c for c in rr0['por_dia'] if c['qtd'] > 0)     # dia com a sugestão
    _override_antigo(r.id, date.fromisoformat(dia_sug['data']), dia_sug['qtd'])

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr = _row(crono, r.id)
    assert rr['editado'] is True
    assert rr['total'] == rr0['total']                   # manual == sugerido
    assert rr.get('override_stale') is not True          # alinhado -> sem aviso

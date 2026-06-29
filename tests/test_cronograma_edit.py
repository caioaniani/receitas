"""Edição manual das células do cronograma (29/06/2026).

Total da receita fica fixo (editar um dia redistribui os outros) e salva
rascunho (CronogramaOverride) que o cronograma/aprovar passam a usar.
"""
from datetime import timedelta

from app.extensions import db
from app.models import CronogramaOverride, Loja, PedidoItem, PedidoLoja, Receita, ReceitaIngrediente
from app.services.cronograma_edit import _redistribuir, editar_celula, resetar_receita
from app.services.previsao_producao import cronograma_producao
from app.utils import hoje


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


# ── redistribuição pura ────────────────────────────────────────────────────
def test_redistribuir_mantem_total():
    assert _redistribuir([10, 20, 0], 0, 30, 30) == [30, 0, 0]
    out = _redistribuir([10, 20, 30], 0, 0, 60)   # tira do dia 0, espalha resto
    assert out[0] == 0 and sum(out) == 60
    out2 = _redistribuir([0, 0, 0], 1, 5, 10)     # outros zerados -> divide igual
    assert out2[1] == 5 and sum(out2) == 10


def test_redistribuir_clampa_no_total():
    out = _redistribuir([10, 10], 0, 999, 20)     # nao passa do total
    assert out == [20, 0]


# ── editar_celula: redistribui, persiste, cronograma reflete ───────────────
def test_editar_celula_persiste_e_cronograma_reflete(app):
    loja = _loja()
    r = _receita_amass()
    _pedido(loja, r, 2, 30)
    _pedido(loja, r, 4, 30)

    base = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr0 = _row(base, r.id)
    total = rr0['total']
    assert total == 60

    # joga tudo no dia 0
    res = editar_celula(r.id, rr0['por_dia'][0]['data'], total,
                        horizonte_dias=7, inicio_offset_dias=0)
    assert res is not None
    assert res['por_dia'][0]['qtd'] == total
    assert sum(c['qtd'] for c in res['por_dia']) == total

    # persistiu como override e o cronograma passa a refletir
    assert CronogramaOverride.query.filter_by(receita_id=r.id).count() == 7
    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr = _row(crono, r.id)
    assert rr['por_dia'][0]['qtd'] == total
    assert rr.get('editado') is True
    assert sum(c['qtd'] for c in rr['por_dia']) == total


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
    n = resetar_receita(r.id, datas)
    assert n == 7
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
    assert CronogramaOverride.query.filter_by(receita_id=r.id).count() == 7

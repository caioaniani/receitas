"""Cronograma de producao POR DIA (previsao_producao.cronograma_producao) +
rota /telaindustriateste.

Distribui a producao por dia acompanhando as entregas (deslocado pelo lead),
descontando o estoque dos primeiros dias.
"""
from datetime import timedelta

from app.extensions import db
from app.models import EstoqueProducao, Loja, PedidoItem, PedidoLoja, Receita
from app.services.previsao_producao import cronograma_producao
from app.utils import hoje


def _receita(nome='Pão'):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0)
    db.session.add(r)
    db.session.commit()
    return r


def _loja(nome='Loja A'):
    loja = Loja(nome=nome, ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


def _pedido(loja, status, data_entrega, receita, qtd):
    p = PedidoLoja(loja_id=loja.id, status=status, data_entrega=data_entrega,
                   data_pedido=data_entrega)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=receita.id,
                              quantidade=qtd))
    db.session.commit()
    return p


def _rec_out(crono, rid):
    return next((x for x in crono['receitas'] if x['receita_id'] == rid), None)


def test_distribui_firme_por_dia(app):
    """Pedido firme cai no dia de producao = dia de entrega (lead 0)."""
    loja = _loja()
    r = _receita()
    _pedido(loja, 'pendente', hoje() + timedelta(days=2), r, 50)

    crono = cronograma_producao(horizonte_dias=7, janela_semanas=6)
    rr = _rec_out(crono, r.id)
    assert rr is not None
    assert rr['por_dia'][2]['qtd'] == 50    # entrega hoje+2, lead 0
    assert rr['por_dia'][0]['qtd'] == 0     # nada hoje
    assert rr['total'] == 50


def test_lead_antecipa_producao(app):
    """Com lead 2, a producao de uma entrega em hoje+2 cai HOJE."""
    loja = _loja()
    r = _receita()
    r.dias_producao = 2
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=2), r, 30)

    crono = cronograma_producao(horizonte_dias=7)
    rr = _rec_out(crono, r.id)
    assert rr['por_dia'][0]['qtd'] == 30    # produz hoje p/ entregar em hoje+2


def test_estoque_cobre_primeiros_dias(app):
    """Estoque pronto desconta dos dias mais proximos primeiro."""
    loja = _loja()
    r = _receita()
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=20))
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 30)
    _pedido(loja, 'pendente', hoje() + timedelta(days=3), r, 30)

    crono = cronograma_producao(horizonte_dias=7)
    rr = _rec_out(crono, r.id)
    assert rr['por_dia'][1]['qtd'] == 10    # 30 - 20 de estoque
    assert rr['por_dia'][3]['qtd'] == 30
    assert rr['total'] == 40


def test_lead_com_estoque_cobre_entregas_proximas(app):
    """Com lead 2 e estoque: o estoque cobre as entregas CRONOLOGICAMENTE mais
    proximas; o que falta numa entrega X e produzido em X-lead."""
    loja = _loja()
    r = _receita()
    r.dias_producao = 2
    db.session.commit()
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=40))
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 30)
    _pedido(loja, 'pendente', hoje() + timedelta(days=2), r, 30)
    _pedido(loja, 'pendente', hoje() + timedelta(days=4), r, 30)

    crono = cronograma_producao(horizonte_dias=7)
    rr = _rec_out(crono, r.id)
    # estoque 40 cobre hoje+1 (30) e parte de hoje+2 (10).
    # hoje+2: falta 20 -> producao em (hoje+2)-2 = hoje.
    # hoje+4: falta 30 -> producao em (hoje+4)-2 = hoje+2.
    assert rr['por_dia'][0]['qtd'] == 20
    assert rr['por_dia'][2]['qtd'] == 30
    assert rr['total'] == 50


def test_rota_telaindustriateste(app, admin_user):
    loja = _loja()
    r = _receita()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 10)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/telaindustriateste/')
    assert resp.status_code == 200
    assert 'cronograma' in resp.get_data(as_text=True).lower()


def test_aprovar_cria_plano_do_dia(app, admin_user):
    from app.models import PlanejamentoProducao
    from app.services.producao import aprovar_plano_do_dia

    loja = _loja()
    r = _receita()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 50)

    plano = aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
    assert plano is not None
    assert plano.data == d2
    assert plano.origem == 'cronograma'
    assert plano.status == 'aprovado'
    assert len(plano.itens) == 1
    it = plano.itens[0]
    assert it.receita_id == r.id
    assert it.qtd_alvo == 50
    assert it.produzido_qtd == 0
    assert PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').count() == 1


def test_reaprovar_substitui(app, admin_user):
    from app.models import PlanejamentoProducao
    from app.services.producao import aprovar_plano_do_dia

    loja = _loja()
    r = _receita()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 50)

    aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
    aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
    # re-aprovar nao duplica
    assert PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').count() == 1


def test_rota_aprovar(app, admin_user):
    from app.models import PlanejamentoProducao

    loja = _loja()
    r = _receita()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 30)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.post('/telaindustriateste/aprovar',
                       data={'data': d2.isoformat(), 'horizonte': 7,
                             'janela': 6})
    assert resp.status_code == 302
    assert PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').first() is not None

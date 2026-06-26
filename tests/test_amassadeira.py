"""Testes do conceito de capacidade da amassadeira (fornadas reais).

- fornadas_amassadeira: conta batidas = ceil(farinha_total / capacidade);
  capacidade 0 = nao usa amassadeira (None).
- detalhe do plano mostra fornadas pra receita de amassadeira e 'nao usa'
  pra receita com capacidade 0.
"""
from app.extensions import db
from app.models import PlanejamentoItem, PlanejamentoProducao, Receita
from app.services.producao import fornadas_amassadeira
from app.utils import hoje


def _receita(peso_base=10000.0, rendimento=200, capacidade=50000):
    r = Receita(nome='Pão Teste', categoria='Paes', rendimento_qtd=rendimento,
                rendimento_unidade='un', peso_base=peso_base,
                capacidade_amassadeira_g=capacidade)
    db.session.add(r)
    db.session.commit()
    return r


def _login(client, admin_user):
    client.post('/auth/login',
                data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)


def test_fornadas_conta_batidas(app):
    r = _receita(peso_base=10000.0, capacidade=50000)
    assert fornadas_amassadeira(r, 6) == 2   # 60kg / 50kg -> 2 batidas
    assert fornadas_amassadeira(r, 5) == 1   # 50kg -> 1
    assert fornadas_amassadeira(r, 1) == 1   # 10kg -> 1 (parcial)


def test_fornadas_capacidade_zero_nao_usa(app):
    r = _receita(capacidade=0)
    assert fornadas_amassadeira(r, 99) is None


def test_fornadas_mult_zero(app):
    r = _receita()
    assert fornadas_amassadeira(r, 0) is None


def test_capacidade_default_50000(app):
    """Receita nova sem informar capacidade -> 50000 (default do modelo)."""
    r = Receita(nome='X', categoria='Paes', rendimento_qtd=10,
                rendimento_unidade='un', peso_base=1000.0)
    db.session.add(r)
    db.session.commit()
    db.session.refresh(r)
    assert r.capacidade_amassadeira_g == 50000


def test_detalhe_mostra_fornadas(app, admin_user):
    r = _receita(peso_base=10000.0, rendimento=200, capacidade=50000)
    plano = PlanejamentoProducao(data=hoje(), nome='P', criado_por=admin_user.id)
    db.session.add(plano)
    db.session.flush()
    db.session.add(PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                                    multiplicador=6))
    db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/producao/%d' % plano.id)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Fornadas' in body
    assert 'Unidades' in body


def test_detalhe_receita_nao_usa_amassadeira(app, admin_user):
    """Moeda (capacidade 0) mostra 'nao usa', nao fornada."""
    r = _receita(peso_base=100.0, rendimento=1, capacidade=0)
    plano = PlanejamentoProducao(data=hoje(), nome='P', criado_por=admin_user.id)
    db.session.add(plano)
    db.session.flush()
    db.session.add(PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                                    multiplicador=274))
    db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/producao/%d' % plano.id)
    assert resp.status_code == 200
    assert 'não usa' in resp.get_data(as_text=True)

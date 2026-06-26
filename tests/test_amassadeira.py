"""Testes da capacidade da amassadeira por MASSA final (fornadas reais).

A amassadeira e limitada pela massa (farinha + agua + tudo), nao pela farinha.
- massa_receita_base: soma todos os ingredientes percentuais + mp_direto.
- fornadas_amassadeira: ceil(massa_total / capacidade); capacidade 0 = nao usa.
- detalhe do plano mostra fornadas / 'nao usa'.
"""
from app.extensions import db
from app.models import (
    PlanejamentoItem,
    PlanejamentoProducao,
    Receita,
    ReceitaIngrediente,
)
from app.services.producao import fornadas_amassadeira, massa_receita_base
from app.utils import hoje


def _receita(peso_base=10000.0, rendimento=200, capacidade=50000,
             pcts=(('Farinha', 100), ('Água', 60))):
    """Receita com ingredientes percentuais. massa_base = peso_base x sum_pct/100.
    Default: farinha 100 + agua 60 => sum_pct 160 => massa = peso_base x 1.6."""
    r = Receita(nome='Pão Teste', categoria='Paes', rendimento_qtd=rendimento,
                rendimento_unidade='un', peso_base=peso_base,
                capacidade_amassadeira_g=capacidade)
    db.session.add(r)
    db.session.flush()
    for nome, pct in pcts:
        db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                          ingrediente_nome=nome, porcentagem=pct))
    db.session.commit()
    return r


def _login(client, admin_user):
    client.post('/auth/login',
                data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)


def test_massa_base_soma_ingredientes(app):
    r = _receita(peso_base=10000.0)            # farinha 100 + agua 60
    assert massa_receita_base(r) == 16000.0    # 10000 x 160/100


def test_massa_inclui_mp_direto(app):
    r = _receita(peso_base=1000.0, pcts=(('Farinha', 100),))
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp_direto',
                                      ingrediente_nome='Semente', porcentagem=50))
    db.session.commit()
    assert massa_receita_base(r) == 1050.0     # 1000 (farinha) + 50 (direto)


def test_fornadas_conta_batidas_por_massa(app):
    # massa_base = 16kg; capacidade 50kg.
    r = _receita(peso_base=10000.0, capacidade=50000)
    assert fornadas_amassadeira(r, 4) == 2     # 64kg / 50kg -> 2
    assert fornadas_amassadeira(r, 3) == 1     # 48kg -> 1
    assert fornadas_amassadeira(r, 1) == 1     # 16kg -> 1 (parcial)


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
                                    multiplicador=4))
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

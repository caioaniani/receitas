"""Etapa 3 (opção B): produzir um item do plano credita estoque pronto e
desconta MP da ficha, proporcional às unidades.
"""
from app.extensions import db
from app.models import (
    EstoqueProducao,
    MateriaPrima,
    PlanejamentoItem,
    PlanejamentoProducao,
    Receita,
    ReceitaIngrediente,
)
from app.services.producao import produzir_item_plano
from app.utils import hoje


def _setup_item(rendimento=10, peso_base=1000.0, farinha_estoque=5000):
    mp = MateriaPrima(nome='Farinha', unidade='g', custo_por_kg=5.0,
                      estoque_atual=farinha_estoque)
    db.session.add(mp)
    db.session.commit()
    r = Receita(nome='Pão Teste', categoria='Paes', rendimento_qtd=rendimento,
                rendimento_unidade='un', peso_base=peso_base)
    db.session.add(r)
    db.session.flush()
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                      ingrediente_nome='Farinha', porcentagem=100))
    plano = PlanejamentoProducao(data=hoje(), origem='cronograma',
                                 status='aprovado', nome='Cronograma')
    db.session.add(plano)
    db.session.flush()
    it = PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                          multiplicador=2, qtd_alvo=20)
    db.session.add(it)
    db.session.commit()
    return r, mp, it


def test_produzir_credita_estoque_e_baixa_mp(app, admin_user):
    r, mp, it = _setup_item(rendimento=10, peso_base=1000.0,
                            farinha_estoque=5000)
    res = produzir_item_plano(it.id, 10, admin_user.id)
    assert res['ok'] is True
    assert res['produzido'] == 10

    ep = EstoqueProducao.query.filter_by(receita_id=r.id).first()
    assert ep is not None and ep.quantidade == 10       # 10 un creditadas

    db.session.refresh(mp)
    assert mp.estoque_atual == 4000   # 10un/10 = 1 base = 1000g farinha; 5000-1000

    db.session.refresh(it)
    assert it.produzido_qtd == 10


def test_produzir_acumula(app, admin_user):
    r, mp, it = _setup_item()
    produzir_item_plano(it.id, 10, admin_user.id)
    produzir_item_plano(it.id, 5, admin_user.id)
    db.session.refresh(it)
    assert it.produzido_qtd == 15


def test_produzir_qtd_invalida(app, admin_user):
    r, mp, it = _setup_item()
    res = produzir_item_plano(it.id, 0, admin_user.id)
    assert res['ok'] is False
    db.session.refresh(it)
    assert it.produzido_qtd == 0


def test_rota_produzir_plano(app, admin_user):
    r, mp, it = _setup_item()
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.post('/padeiro-testes/produzir-plano/%d' % it.id,
                       data={'unidades': 10})
    assert resp.status_code == 302
    db.session.refresh(it)
    assert it.produzido_qtd == 10


def test_padeiro_testes_mostra_producao_do_dia(app, admin_user):
    r, mp, it = _setup_item()
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/padeiro-testes/')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Produção do dia' in body
    assert 'Pão Teste' in body

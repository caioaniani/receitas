"""Testes da Fatia 3 — ordem de compra de MP consolidada por fornecedor.

- ordem_compra_consolidada: agrupa por fornecedor, calcula 'a comprar'
  (deficit considerando estoque) e o custo da compra.
- rota /producao/<id>/lista-compras renderiza a ordem de compra.
"""
from app.extensions import db
from app.models import (
    MateriaPrima,
    PlanejamentoItem,
    PlanejamentoProducao,
    Receita,
    ReceitaIngrediente,
)
from app.services.producao import ordem_compra_consolidada
from app.utils import hoje


def _receita_com_mp(itens_mp):
    """itens_mp = [(ingrediente_nome, porcentagem)]. peso_base 1000."""
    r = Receita(nome='Pão Teste', categoria='Paes', rendimento_qtd=10,
                rendimento_unidade='un', peso_base=1000.0)
    db.session.add(r)
    db.session.flush()
    for nome, pct in itens_mp:
        db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                          ingrediente_nome=nome, porcentagem=pct))
    db.session.commit()
    return r


def test_ordem_compra_agrupa_por_fornecedor(app):
    db.session.add_all([
        MateriaPrima(nome='Farinha', unidade='g', custo_por_kg=5.0,
                     estoque_atual=0, fornecedor='Moinho X'),
        MateriaPrima(nome='Sal', unidade='g', custo_por_kg=2.0,
                     estoque_atual=0),   # sem fornecedor
    ])
    db.session.commit()
    r = _receita_com_mp([('Farinha', 100), ('Sal', 2)])

    ordem = ordem_compra_consolidada([{'receita_id': r.id, 'multiplicador': 2}])
    nomes_forn = [f['nome'] for f in ordem['fornecedores']]
    assert 'Moinho X' in nomes_forn
    assert 'Sem fornecedor' in nomes_forn
    assert nomes_forn[-1] == 'Sem fornecedor'        # vai por ultimo

    moinho = next(f for f in ordem['fornecedores'] if f['nome'] == 'Moinho X')
    farinha = moinho['itens'][0]
    assert farinha['nome'] == 'Farinha'
    assert farinha['quantidade'] == 2000             # 100% de 1000g x2
    assert farinha['comprar'] == 2000                # estoque 0
    assert abs(farinha['custo_compra'] - 10.0) < 0.01  # 2kg x R$5


def test_ordem_compra_deficit_considera_estoque(app):
    db.session.add(MateriaPrima(nome='Farinha', unidade='g', custo_por_kg=5.0,
                                estoque_atual=500, fornecedor='Moinho X'))
    db.session.commit()
    r = _receita_com_mp([('Farinha', 100)])

    ordem = ordem_compra_consolidada([{'receita_id': r.id, 'multiplicador': 1}])
    farinha = ordem['fornecedores'][0]['itens'][0]
    assert farinha['quantidade'] == 1000             # 1 fornada
    assert farinha['comprar'] == 500                 # 1000 - 500 em estoque
    assert abs(ordem['total_compra'] - 2.5) < 0.01   # 0.5kg x R$5


def test_rota_ordem_compra_renderiza(app, admin_user):
    db.session.add(MateriaPrima(nome='Farinha', unidade='g', custo_por_kg=5.0,
                                estoque_atual=0, fornecedor='Moinho X'))
    db.session.commit()
    r = _receita_com_mp([('Farinha', 100)])
    plano = PlanejamentoProducao(data=hoje(), nome='Plano teste',
                                 criado_por=admin_user.id)
    db.session.add(plano)
    db.session.flush()
    db.session.add(PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                                    multiplicador=1))
    db.session.commit()

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/producao/%d/lista-compras' % plano.id)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Ordem de Compra' in body
    assert 'Moinho X' in body
    assert 'Farinha' in body

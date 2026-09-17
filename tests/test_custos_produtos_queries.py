"""Regressão GESTAO-PADARIA-2V: custos não consultam cada composição isoladamente."""
from contextlib import contextmanager

import pytest
from sqlalchemy import event

from app.extensions import db
from app.models import MateriaPrima, Produto, ProdutoItem, Receita, ReceitaIngrediente
from app.services.custos import calcular_custos_produtos, calcular_custos_receitas


@contextmanager
def _selects():
    consultas = []

    def registrar(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            consultas.append(statement)

    event.listen(db.engine, 'before_cursor_execute', registrar)
    try:
        yield consultas
    finally:
        event.remove(db.engine, 'before_cursor_execute', registrar)


def _catalogo(quantidade):
    """Componentes distintos por cesta expõem lazy loads mesmo sem cache prévio."""
    custos_esperados = {}
    ids = []
    for i in range(quantidade):
        mp = MateriaPrima(nome=f'Farinha N1 {i}', unidade='g', custo_por_kg=10)
        receita = Receita(nome=f'Pão N1 {i}', categoria='Teste', rendimento_qtd=1,
                          rendimento_unidade='un', peso_base=1000, peso_unitario=100)
        revenda = Produto(nome=f'Revenda N1 {i}', ativo=True, custo_direto=4)
        cesta = Produto(nome=f'Cesta N1 {i}', ativo=True, custo_embalagem=1)
        externa = Produto(nome=f'Cesta externa N1 {i}', ativo=True, custo_embalagem=2)
        db.session.add_all([mp, receita, revenda, cesta, externa])
        db.session.flush()
        db.session.add(ReceitaIngrediente(
            receita_id=receita.id, ingrediente_nome=mp.nome,
            tipo='mp_direto', porcentagem=1000))
        db.session.add_all([
            ProdutoItem(produto_id=cesta.id, tipo='receita', receita_id=receita.id,
                        item_nome=f'Nome antigo pão {i}', quantidade=2),
            ProdutoItem(produto_id=cesta.id, tipo='mp', materia_prima_id=mp.id,
                        item_nome=f'Nome antigo farinha {i}', quantidade=100),
            ProdutoItem(produto_id=cesta.id, tipo='produto',
                        produto_componente_id=revenda.id,
                        item_nome=f'Nome antigo revenda {i}', quantidade=2),
            ProdutoItem(produto_id=externa.id, tipo='produto',
                        produto_componente_id=cesta.id,
                        item_nome=f'Nome antigo cesta {i}', quantidade=3),
        ])
        custos_esperados.update({revenda.nome: 4, cesta.nome: 12, externa.nome: 38})
        ids.append(cesta.id)
    db.session.commit()
    # Nenhum ORM criado pelo fixture pode mascarar consultas via identity map.
    db.session.remove()
    return ids, custos_esperados


@pytest.mark.parametrize('quantidade', [3, 20])
def test_custos_carregam_composicoes_e_vinculos_em_lote(app, quantidade):
    _, esperado = _catalogo(quantidade)
    resultado = calcular_custos_receitas()
    db.session.remove()
    with _selects() as consultas:
        custos = calcular_custos_produtos(resultado['custos'], resultado['mp_info'])
    assert custos == esperado
    assert len(consultas) <= 6, f'{len(consultas)} consultas para {quantidade} cestas'
    assert sum('FROM produto_item' in q for q in consultas) == 1


@pytest.mark.parametrize('ativo', [True, False])
def test_detalhe_carrega_composicoes_do_catalogo_em_lote(app, admin_user, ativo):
    cliente = app.test_client()
    with cliente.session_transaction() as sess:
        sess['_user_id'] = str(admin_user.id)
        sess['_fresh'] = True
    ids, _ = _catalogo(20)
    # A ficha também permite consultar produtos arquivados, fora do índice ativo.
    produto = db.session.get(Produto, ids[0])
    produto.ativo = ativo
    db.session.commit()
    db.session.remove()
    with _selects() as consultas:
        resp = cliente.get(f'/produtos/{ids[0]}')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'Pão N1 0' in html and 'Farinha N1 0' in html and 'Revenda N1 0' in html
    assert 'Nome antigo' not in html
    assert 'R$ 11,00' in html  # composição, sem a embalagem calculada pelo JavaScript
    # O menu administrativo faz uma contagem de órfãos independente da ficha.
    composicoes = [q for q in consultas if 'FROM produto_item' in q
                   and not q.lstrip().lower().startswith('select count(')]
    assert len(composicoes) <= 2, f'{len(composicoes)} consultas de composição'

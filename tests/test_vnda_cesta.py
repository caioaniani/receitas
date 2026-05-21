"""Smoke test desempacotamento de cesta no calculo de sugestao de pedido
via VNDA API direta. Mock da API VNDA pra nao depender de rede."""
from datetime import date
from unittest.mock import patch


def test_vnda_api_desempacota_cesta(app, admin_user, loja, catalogo):
    """Family Box × 5 (com 3 receitas dentro: 2 Croissant + 4 Pao + 1 MP)
    via VNDA deve gerar: 10 Croissant + 20 Pao + 5 MP (multiplicado)."""
    from app.extensions import db
    from app.models import Produto, ProdutoItem, Receita, VndaProdutoMap
    from app.services.vendas_manuais import _agregar_vendas_vnda_api

    # Cria mais 2 receitas (alem da receita do catalogo)
    pao = Receita(nome='Pao Frances Teste', categoria='Paes',
                  rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0)
    db.session.add(pao)
    db.session.flush()

    # Cria produto cesta "Family Box" com 3 componentes
    cesta = Produto(nome='Family Box', ativo=True)
    db.session.add(cesta)
    db.session.flush()
    db.session.add_all([
        ProdutoItem(produto_id=cesta.id, tipo='receita',
                    item_nome=catalogo['receita'].nome, quantidade=2),  # 2 Croissant
        ProdutoItem(produto_id=cesta.id, tipo='receita',
                    item_nome=pao.nome, quantidade=4),  # 4 Pao
        ProdutoItem(produto_id=cesta.id, tipo='mp',
                    item_nome=catalogo['mp'].nome, quantidade=1),  # 1 MP
    ])
    # Mapeia VNDA → Family Box
    db.session.add(VndaProdutoMap(
        vnda_nome='Family Box', produto_id=cesta.id,
        confirmado_em=date.today(),
    ))
    db.session.commit()

    # Mock da API: 1 pedido com 5 Family Box em 2026-04-15
    fake_orders = [{
        'status': 'confirmed',
        'expected_delivery_date': '2026-04-15',
        'items': [{
            'product_name': 'Family Box',
            'quantity': 5,
        }],
    }]

    def fake_data(o):
        return date.fromisoformat(o['expected_delivery_date'])

    with patch('app.services.vnda._buscar_pedidos_janela',
                return_value=fake_orders), \
         patch('app.services.vnda._extrair_data_entrega',
                side_effect=fake_data):
        vendas, aviso = _agregar_vendas_vnda_api(
            date(2026, 4, 1), date(2026, 4, 30))

    assert aviso is None
    # 5 cestas × 2 Croissant = 10
    assert vendas.get(('receita', catalogo['receita'].id)) == 10
    # 5 × 4 Pao = 20
    assert vendas.get(('receita', pao.id)) == 20
    # 5 × 1 MP = 5
    assert vendas.get(('mp', catalogo['mp'].id)) == 5
    # NAO deve ter o produto cesta como entrada propria
    assert ('produto', cesta.id) not in vendas


def test_vnda_api_produto_simples_nao_explode(app, admin_user, loja, catalogo):
    """Produto sem componentes vai como produto direto (nao explode)."""
    from unittest.mock import patch

    from app.extensions import db
    from app.models import Produto, VndaProdutoMap
    from app.services.vendas_manuais import _agregar_vendas_vnda_api

    simples = Produto(nome='Granola 500g', ativo=True)
    db.session.add(simples)
    db.session.flush()
    db.session.add(VndaProdutoMap(
        vnda_nome='Granola 500g', produto_id=simples.id,
        confirmado_em=date.today(),
    ))
    db.session.commit()

    fake = [{'status': 'confirmed', 'expected_delivery_date': '2026-04-10',
             'items': [{'product_name': 'Granola 500g', 'quantity': 3}]}]

    with patch('app.services.vnda._buscar_pedidos_janela', return_value=fake), \
         patch('app.services.vnda._extrair_data_entrega',
                side_effect=lambda o: date.fromisoformat(o['expected_delivery_date'])):
        vendas, _ = _agregar_vendas_vnda_api(
            date(2026, 4, 1), date(2026, 4, 30))

    assert vendas.get(('produto', simples.id)) == 3

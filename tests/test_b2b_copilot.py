"""Smoke tests do fluxo B2B via copilot.

Cobre: enricher resolve cliente cadastrado, item resolve preco do cadastro,
executor cria venda + baixa estoque, cliente_nome novo vira avulso.
"""


def test_enricher_resolve_cliente_e_preco(app, admin_user, catalogo):
    """Cliente cadastrado vira cliente_id; preco vem de Receita.preco_venda."""
    from app.extensions import db
    from app.models import ClienteB2B
    from app.services import copilot

    cli = ClienteB2B(nome='Hotel Brisamar', desconto_percentual=10)
    db.session.add(cli)
    catalogo['receita'].preco_venda = 10.0
    db.session.commit()

    out = copilot._enriquecer_criar_venda_b2b({
        'cliente_nome': 'brisamar',  # fuzzy
        'itens': [{'nome': 'Croissant', 'quantidade': 3}],
    })
    assert out['cliente_id'] == cli.id
    assert out['cliente_nome_resolvido'] == 'Hotel Brisamar'
    assert out['cliente_avulso'] is False
    # Preco com 10% desconto: 10 * 0.9 = 9.0
    assert out['itens'][0]['preco_unitario'] == 9.0
    # Subtotal: 3 * 9 = 27
    assert out['itens'][0]['subtotal'] == 27.0
    assert out['total'] == 27.0


def test_enricher_cliente_nao_cadastrado_avulso(app, admin_user, catalogo):
    """Cliente que nao existe = avulso. cliente_id=None, mantem nome."""
    from app.extensions import db
    from app.services import copilot

    catalogo['receita'].preco_venda = 8.0
    db.session.commit()

    out = copilot._enriquecer_criar_venda_b2b({
        'cliente_nome': 'Restaurante Inexistente',
        'itens': [{'nome': 'Croissant', 'quantidade': 2}],
    })
    assert out['cliente_id'] is None
    assert out['cliente_avulso'] is True
    assert out['cliente_nome_resolvido'] == 'Restaurante Inexistente'
    # Sem desconto, preco = 8
    assert out['itens'][0]['preco_unitario'] == 8.0


def test_executor_cria_venda_e_baixa_freezer(app, admin_user, catalogo):
    """Executor end-to-end: cria VendaB2B, baixa EstoqueProducao."""
    from app.extensions import db
    from app.models import EstoqueProducao, VendaB2B
    from app.services import copilot

    ep = EstoqueProducao(receita_id=catalogo['receita'].id, quantidade=20)
    db.session.add(ep)
    catalogo['receita'].preco_venda = 5.0
    db.session.commit()

    # Simula params como vem do enricher
    params = copilot._enriquecer_criar_venda_b2b({
        'cliente_nome': 'Avulso Teste',
        'itens': [{'nome': 'Croissant', 'quantidade': 4}],
    })
    resultado = copilot.executar_criar_venda_b2b(params, admin_user)
    assert resultado['ok'] is True
    assert resultado['itens_salvos'] == 1
    venda = VendaB2B.query.get(resultado['venda_id'])
    assert venda.valor_total == 20.0
    db.session.refresh(ep)
    assert ep.quantidade == 16


def test_executor_criar_cliente_b2b_idempotente(app, admin_user):
    """Criar cliente com nome ja existente retorna o existente."""
    from app.extensions import db
    from app.models import ClienteB2B
    from app.services import copilot

    existente = ClienteB2B(nome='Padaria do Joao')
    db.session.add(existente)
    db.session.commit()
    out = copilot.executar_criar_cliente_b2b(
        {'nome': 'Padaria do Joao'}, admin_user,
    )
    assert out['ok'] is True
    assert out['cliente_id'] == existente.id
    assert out.get('duplicado') is True
    # So tem 1 cliente
    assert ClienteB2B.query.count() == 1


def test_executor_item_nao_resolvido_aborta(app, admin_user):
    """Sem item resolvido, executor devolve erro claro (nao crasha)."""
    from app.services import copilot
    params = {
        'cliente_nome_resolvido': 'Avulso',
        'cliente_avulso': True,
        'itens': [{'nome_original': 'XYZ inexistente',
                   'quantidade': 1, 'preco_unitario': 5,
                   'desconto_percentual': 0, 'resolvido': None}],
        'parcelas': [],
    }
    out = copilot.executar_criar_venda_b2b(params, admin_user)
    assert out['ok'] is False
    assert 'XYZ' in out['erro']

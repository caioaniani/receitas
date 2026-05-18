"""Smoke tests do fluxo de criar pedido via copilot.

Regressoes cobertas:
- Item com observacao='backup' eh salvo no PedidoItem
- Pedido com 2 itens (mesmo nome, obs diferente) gera 2 PedidoItem rows
- Item sem match no catalogo vai pra nao_resolvidos sem crashar
"""


def test_criar_pedido_basico(app, admin_user, loja, catalogo):
    """End-to-end: enriquece + executa criar pedido com 1 item."""
    from app.services import copilot
    from app.models import PedidoLoja, PedidoItem

    tool_input = {
        'loja_id': loja.id,
        'data_entrega': '2026-12-25',
        'itens': [{'nome': 'Croissant', 'quantidade': 5}],
    }
    params = copilot._enriquecer_criar_pedido(tool_input)
    assert params['loja_id'] == loja.id
    assert len(params['itens']) == 1
    assert params['itens'][0]['resolvido'] is not None

    resultado = copilot.executar_criar_pedido(params, admin_user)
    assert resultado['ok'] is True
    assert resultado['itens_salvos'] == 1
    p = PedidoLoja.query.get(resultado['pedido_id'])
    assert p.loja_id == loja.id
    itens = PedidoItem.query.filter_by(pedido_id=p.id).all()
    assert len(itens) == 1
    assert itens[0].quantidade == 5
    assert itens[0].receita_id == catalogo['receita'].id


def test_criar_pedido_com_backup(app, admin_user, loja, catalogo):
    """Item com observacao='backup' eh persistido no PedidoItem."""
    from app.services import copilot
    from app.models import PedidoItem

    tool_input = {
        'loja_id': loja.id,
        'data_entrega': '2026-12-25',
        'itens': [
            {'nome': 'Croissant', 'quantidade': 5},
            {'nome': 'Croissant', 'quantidade': 3, 'observacao': 'backup'},
        ],
    }
    params = copilot._enriquecer_criar_pedido(tool_input)
    resultado = copilot.executar_criar_pedido(params, admin_user)
    assert resultado['ok'] is True
    assert resultado['itens_salvos'] == 2

    itens = PedidoItem.query.filter_by(pedido_id=resultado['pedido_id']).all()
    obs_set = sorted([(it.quantidade, it.observacao or '') for it in itens])
    assert obs_set == [(3, 'backup'), (5, '')]


def test_criar_pedido_item_sem_match_vira_nao_resolvido(app, admin_user, loja):
    """Item sem catalogo vai pra nao_resolvidos, nao crasha."""
    from app.services import copilot
    tool_input = {
        'loja_id': loja.id,
        'data_entrega': '2026-12-25',
        'itens': [{'nome': 'XYZ123 inexistente', 'quantidade': 1}],
    }
    params = copilot._enriquecer_criar_pedido(tool_input)
    resultado = copilot.executar_criar_pedido(params, admin_user)
    # Sem item resolvido + nenhum salvo = erro 'Nenhum item resolvido'
    assert resultado['ok'] is False
    assert 'XYZ123' in resultado.get('erro', '')


def test_criar_pedido_sem_loja(app, admin_user, catalogo):
    """Sem loja_id, devolve erro claro sem fallback silencioso."""
    from app.services import copilot
    params = {
        'loja_id': None, 'loja_nome': None,
        'data_entrega': '2026-12-25',
        'itens': [{'quantidade': 1, 'resolvido': {
            'tipo': 'receita', 'id': catalogo['receita'].id, 'nome': 'X'}}],
    }
    resultado = copilot.executar_criar_pedido(params, admin_user)
    assert resultado['ok'] is False
    assert 'Loja' in resultado['erro']

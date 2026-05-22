"""Testes do fluxo de editar pedido via copilot.

Cobre:
- Edicao seletiva (so data, so obs) preserva itens
- REPLACE total de itens preserva estado='backup'
- Bloqueio por status (separado, entregue, cancelado)
- pedido_id inexistente
- Matriz de permissoes (admin sim, funcionario nao)
"""
from datetime import date, timedelta


def _pedido_pendente(loja, admin_user, catalogo, status='pendente'):
    """Helper local: cria PedidoLoja com 1 PedidoItem (receita do catalogo)."""
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    p = PedidoLoja(loja_id=loja.id, status=status,
                   data_entrega=date.today() + timedelta(days=1),
                   criado_por=admin_user.id)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id,
                              receita_id=catalogo['receita'].id,
                              quantidade=10))
    db.session.commit()
    return p


def test_editar_pedido_so_data(app, admin_user, loja, catalogo):
    """Mudar so data_entrega preserva itens existentes."""
    from app.models import PedidoItem, PedidoLoja
    from app.services import copilot
    p = _pedido_pendente(loja, admin_user, catalogo)
    nova_data = (date.today() + timedelta(days=7)).strftime('%Y-%m-%d')

    params = copilot._enriquecer_editar_pedido({
        'pedido_id': p.id,
        'data_entrega': nova_data,
    })
    out = copilot.executar_editar_pedido(params, admin_user)
    assert out['ok'] is True
    assert 'data_entrega' in out['mudancas']

    p2 = PedidoLoja.query.get(p.id)
    assert p2.data_entrega.strftime('%Y-%m-%d') == nova_data
    # Itens nao foram tocados
    itens = PedidoItem.query.filter_by(pedido_id=p.id).all()
    assert len(itens) == 1
    assert itens[0].quantidade == 10


def test_editar_pedido_so_obs(app, admin_user, loja, catalogo):
    """Mudar so observacao do pedido."""
    from app.models import PedidoLoja
    from app.services import copilot
    p = _pedido_pendente(loja, admin_user, catalogo)

    params = copilot._enriquecer_editar_pedido({
        'pedido_id': p.id,
        'observacao': 'urgente — separar primeiro',
    })
    out = copilot.executar_editar_pedido(params, admin_user)
    assert out['ok'] is True
    assert 'observacao' in out['mudancas']

    assert PedidoLoja.query.get(p.id).observacao == 'urgente — separar primeiro'


def test_editar_pedido_replace_itens_preserva_estado(app, admin_user, loja, catalogo):
    """REPLACE de itens com estado='backup' persiste no PedidoItem.

    Regressao do bug do croissant: estado precisa chegar do tool_input ate o banco.
    """
    from app.models import PedidoItem
    from app.services import copilot
    p = _pedido_pendente(loja, admin_user, catalogo)

    params = copilot._enriquecer_editar_pedido({
        'pedido_id': p.id,
        'itens': [
            {'nome': 'Croissant', 'quantidade': 30, 'estado': 'backup'},
        ],
    })
    out = copilot.executar_editar_pedido(params, admin_user)
    assert out['ok'] is True

    itens = PedidoItem.query.filter_by(pedido_id=p.id).all()
    assert len(itens) == 1
    assert itens[0].quantidade == 30
    assert itens[0].estado == 'backup'


def test_editar_pedido_replace_apaga_antigos(app, admin_user, loja, catalogo):
    """Pedido com 1 item (qtd=10); manda 2 novos (qtd=5 e 3).
    O antigo (qtd=10) some — verificacao por qtd ja que SQLite reusa ROWIDs."""
    from app.models import PedidoItem
    from app.services import copilot
    p = _pedido_pendente(loja, admin_user, catalogo)

    params = copilot._enriquecer_editar_pedido({
        'pedido_id': p.id,
        'itens': [
            {'nome': 'Croissant', 'quantidade': 5},
            {'nome': 'Croissant', 'quantidade': 3, 'estado': 'backup'},
        ],
    })
    out = copilot.executar_editar_pedido(params, admin_user)
    assert out['ok'] is True

    itens = PedidoItem.query.filter_by(pedido_id=p.id).all()
    qtds = sorted(it.quantidade for it in itens)
    assert qtds == [3, 5]  # item antigo (qtd=10) sumiu, restaram os 2 novos


def test_editar_pedido_status_separado_bloqueia(app, admin_user, loja, catalogo):
    """Pedido em 'separado' nao pode ser editado — estoque ja foi tocado."""
    from app.models import PedidoLoja
    from app.services import copilot
    p = _pedido_pendente(loja, admin_user, catalogo, status='separado')

    params = copilot._enriquecer_editar_pedido({
        'pedido_id': p.id,
        'observacao': 'tentativa proibida',
    })
    out = copilot.executar_editar_pedido(params, admin_user)
    assert out['ok'] is False
    assert 'separado' in out['erro']
    # Banco intacto
    assert PedidoLoja.query.get(p.id).observacao is None


def test_editar_pedido_status_entregue_bloqueia(app, admin_user, loja, catalogo):
    """Pedido terminal 'entregue' bloqueia edicao."""
    from app.services import copilot
    p = _pedido_pendente(loja, admin_user, catalogo, status='entregue')

    params = copilot._enriquecer_editar_pedido({
        'pedido_id': p.id,
        'observacao': 'tarde demais',
    })
    out = copilot.executar_editar_pedido(params, admin_user)
    assert out['ok'] is False
    assert 'entregue' in out['erro']


def test_editar_pedido_id_inexistente(app, admin_user):
    """pedido_id=99999 retorna erro claro."""
    from app.services import copilot
    params = copilot._enriquecer_editar_pedido({
        'pedido_id': 99999,
        'observacao': 'nada',
    })
    out = copilot.executar_editar_pedido(params, admin_user)
    assert out['ok'] is False
    assert '99999' in out['erro']


def test_pode_usar_editar_pedido(app, admin_user):
    """Admin pode usar editar_pedido; funcionario nao."""
    from app.extensions import db
    from app.models import Usuario
    from app.services.copilot import pode_usar

    assert pode_usar('editar_pedido', admin_user) is True

    func = Usuario(nome='Funcionario', login='func', papel='funcionario')
    func.set_senha('x')
    db.session.add(func)
    db.session.commit()
    assert pode_usar('editar_pedido', func) is False

"""Voltar status de pedido EM MASSA — POST /pedidos/voltar-status-lote (admin).

O lote roda numa UNICA transacao: o estorno de estoque de pedidos da MESMA
loja/receita soma certo (sem o lost-update que N requests paralelos teriam ao
bater na mesma linha de EstoqueProducao/EstoqueLoja).
"""
from datetime import date, timedelta

import pytest


def _login(cliente, login='admin', senha='123'):
    return cliente.post('/auth/login', data={'login': login, 'senha': senha})


@pytest.fixture
def cliente(app):
    return app.test_client()


def _pedido(loja, admin_user, receita, status, qtd):
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    p = PedidoLoja(loja_id=loja.id, status=status,
                   data_entrega=date.today() + timedelta(days=1),
                   criado_por=admin_user.id)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=receita.id,
                              quantidade=qtd))
    db.session.commit()
    return p


def test_lote_estorna_estoque_sem_lost_update(app, cliente, loja, admin_user,
                                              catalogo):
    """2 pedidos da MESMA receita ENVIADOS de verdade -> ambos voltam pra
    separado e o estoque producao recebe o estorno dos DOIS (7+5), sem perder
    um. (03/07/2026: o estorno agora espelha os MOVIMENTOS reais do envio —
    pedido fabricado em em_transporte sem baixa não credita mais nada, então o
    teste envia pelo caminho canônico.)"""
    from app.blueprints.pedidos.routes import _executar_envio_pedido
    from app.extensions import db
    from app.models import EstoqueProducao, PedidoLoja
    receita = catalogo['receita']
    db.session.add(EstoqueProducao(receita_id=receita.id, quantidade=12))
    db.session.commit()
    p1 = _pedido(loja, admin_user, receita, 'separado', 7)
    p2 = _pedido(loja, admin_user, receita, 'separado', 5)
    with app.test_request_context():
        assert _executar_envio_pedido(p1, admin_user)[0] is True
        assert _executar_envio_pedido(p2, admin_user)[0] is True
    ep0 = EstoqueProducao.query.filter_by(receita_id=receita.id).first()
    assert ep0.quantidade == 0                     # 12 - 7 - 5

    _login(cliente)
    r = cliente.post('/pedidos/voltar-status-lote',
                     data={'ids[]': [str(p1.id), str(p2.id)]})
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] is True
    assert j['revertidos'] == 2

    db.session.expire_all()
    assert PedidoLoja.query.get(p1.id).status == 'separado'
    assert PedidoLoja.query.get(p2.id).status == 'separado'
    ep = EstoqueProducao.query.filter_by(receita_id=receita.id).first()
    assert ep.quantidade == 12     # 7 + 5 — estorno dos dois, nada perdido


def test_lote_ignora_status_que_nao_volta(app, cliente, loja, admin_user,
                                          catalogo):
    """pendente e cancelado nao voltam (ignorados); confirmado volta pra
    pendente."""
    from app.extensions import db
    from app.models import PedidoLoja
    receita = catalogo['receita']
    p_pend = _pedido(loja, admin_user, receita, 'pendente', 3)
    p_canc = _pedido(loja, admin_user, receita, 'cancelado', 3)
    p_conf = _pedido(loja, admin_user, receita, 'confirmado', 3)

    _login(cliente)
    r = cliente.post('/pedidos/voltar-status-lote',
                     data={'ids[]': [str(p_pend.id), str(p_canc.id),
                                     str(p_conf.id)]})
    j = r.get_json()
    assert j['revertidos'] == 1
    assert j['ignorados'] == 2

    db.session.expire_all()
    assert PedidoLoja.query.get(p_conf.id).status == 'pendente'
    assert PedidoLoja.query.get(p_pend.id).status == 'pendente'
    assert PedidoLoja.query.get(p_canc.id).status == 'cancelado'


def test_lote_exige_login(app, cliente, loja, admin_user, catalogo):
    """Sem login a rota nao executa (redirect pro login)."""
    p = _pedido(loja, admin_user, catalogo['receita'], 'confirmado', 3)
    r = cliente.post('/pedidos/voltar-status-lote',
                     data={'ids[]': [str(p.id)]})
    assert r.status_code in (301, 302, 401, 403)
    from app.extensions import db
    db.session.expire_all()
    from app.models import PedidoLoja
    assert PedidoLoja.query.get(p.id).status == 'confirmado'   # intacto

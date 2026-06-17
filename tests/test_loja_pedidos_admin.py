"""Tela admin de acompanhamento dos pedidos do site (Fase 3).

Lista + detalhe + cancelar. Owner-only (mesma seção do Catálogo do site).
"""
from decimal import Decimal


def _owner(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Dono', login='dono', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def _admin_nao_owner(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Ger', login='ger', papel='admin', is_owner=False)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def _pedido(db, codigo=None, status='aguardando_pagamento', nome='Maria'):
    from app.models import PedidoOnline, PedidoOnlineItem
    p = PedidoOnline(nome_cliente=nome, email_cliente='m@x.com',
                     telefone_cliente='11999', modo_entrega='retirada',
                     status=status, subtotal=Decimal('20'),
                     valor_total=Decimal('20'))
    if codigo:
        p.codigo = codigo
    p.itens.append(PedidoOnlineItem(
        kind='produto', nome='Box Mimo', preco_unitario=Decimal('20'),
        quantidade=1, subtotal=Decimal('20')))
    db.session.add(p)
    db.session.commit()
    return p


def test_lista_pedidos_owner_200(app):
    from app.extensions import db
    c = _owner(app)
    _pedido(db, nome='Cliente Teste')
    r = c.get('/admin/loja-online/pedidos')
    assert r.status_code == 200
    assert b'Cliente Teste' in r.data
    assert b'Pedidos do site' in r.data


def test_lista_filtra_por_status(app):
    from app.extensions import db
    c = _owner(app)
    _pedido(db, codigo='AAAA0001', status='aguardando_pagamento')
    _pedido(db, codigo='BBBB0002', status='cancelado')
    r = c.get('/admin/loja-online/pedidos?status=cancelado')
    assert r.status_code == 200
    assert b'BBBB0002' in r.data
    assert b'AAAA0001' not in r.data


def test_detalhe_pedido_mostra_itens(app):
    from app.extensions import db
    c = _owner(app)
    p = _pedido(db, codigo='CCCC0003')
    r = c.get(f'/admin/loja-online/pedidos/{p.codigo}')
    assert r.status_code == 200
    assert b'Box Mimo' in r.data
    assert b'CCCC0003' in r.data


def test_cancelar_pedido(app):
    from app.extensions import db
    from app.models import PedidoOnline
    c = _owner(app)
    p = _pedido(db, codigo='DDDD0004')
    r = c.post(f'/admin/loja-online/pedidos/{p.codigo}/cancelar',
               follow_redirects=False)
    assert r.status_code == 302
    atual = PedidoOnline.query.filter_by(codigo='DDDD0004').first()
    assert atual.status == 'cancelado'
    assert atual.cancelado_em is not None


def test_cancelar_pedido_entregue_nao_muda(app):
    from app.extensions import db
    from app.models import PedidoOnline
    c = _owner(app)
    p = _pedido(db, codigo='EEEE0005', status='entregue')
    c.post(f'/admin/loja-online/pedidos/{p.codigo}/cancelar',
           follow_redirects=True)
    atual = PedidoOnline.query.filter_by(codigo='EEEE0005').first()
    assert atual.status == 'entregue'  # não cancelou


def test_nao_owner_bloqueado(app):
    from app.extensions import db
    c = _admin_nao_owner(app)
    _pedido(db)
    r = c.get('/admin/loja-online/pedidos')
    assert r.status_code in (302, 403)  # owner_required barra


def test_pedido_inexistente_404(app):
    c = _owner(app)
    assert c.get('/admin/loja-online/pedidos/NAOEXISTE').status_code == 404

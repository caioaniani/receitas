"""Mudança de status admin → cliente (Fase 6 — PR 4).

A rota `loja_online_pedido_status` avança o status do pedido (pago →
em_preparo → a_caminho → entregue) e dispara o e-mail 'a caminho' na
transição certa.
"""
from decimal import Decimal
from unittest.mock import patch


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


def _pedido(db, codigo='ST0001', status='pago'):
    from app.models import Cliente, PedidoOnline
    cli = Cliente(nome='X', email='cli@x.com')
    db.session.add(cli)
    db.session.flush()
    p = PedidoOnline(codigo=codigo, cliente_id=cli.id,
                     nome_cliente='X', email_cliente='cli@x.com',
                     modo_entrega='retirada', status=status,
                     subtotal=Decimal('10'), frete_valor=Decimal('0'),
                     valor_total=Decimal('10'))
    db.session.add(p)
    db.session.commit()
    return p


def test_pago_pra_em_preparo(app):
    from app.extensions import db
    c = _owner(app)
    _pedido(db, codigo='ST0001', status='pago')
    r = c.post('/admin/loja-online/pedidos/ST0001/status',
                data={'novo_status': 'em_preparo'}, follow_redirects=False)
    assert r.status_code == 302
    with app.app_context():
        from app.models import PedidoOnline
        p = PedidoOnline.query.filter_by(codigo='ST0001').first()
        assert p.status == 'em_preparo'


def test_avanca_pra_a_caminho_dispara_email(app):
    from app.extensions import db
    c = _owner(app)
    _pedido(db, codigo='ST0002', status='em_preparo')
    with patch('app.services.email.disponivel', return_value=True), \
         patch('app.services.email.enviar_pedido_a_caminho') as ev:
        r = c.post('/admin/loja-online/pedidos/ST0002/status',
                    data={'novo_status': 'a_caminho'})
    assert r.status_code == 302
    ev.assert_called_once()
    with app.app_context():
        from app.models import PedidoOnline
        assert PedidoOnline.query.filter_by(codigo='ST0002').first().status == 'a_caminho'


def test_a_caminho_pra_entregue_nao_dispara_email(app):
    from app.extensions import db
    c = _owner(app)
    _pedido(db, codigo='ST0003', status='a_caminho')
    with patch('app.services.email.enviar_pedido_a_caminho') as ev:
        c.post('/admin/loja-online/pedidos/ST0003/status',
               data={'novo_status': 'entregue'})
    ev.assert_not_called()


def test_status_invalido_rejeitado(app):
    from app.extensions import db
    c = _owner(app)
    _pedido(db, codigo='ST0004', status='pago')
    r = c.post('/admin/loja-online/pedidos/ST0004/status',
                data={'novo_status': 'invalido'}, follow_redirects=False)
    assert r.status_code == 302  # flash + redirect, não 500
    with app.app_context():
        from app.models import PedidoOnline
        # Status não mudou
        assert PedidoOnline.query.filter_by(codigo='ST0004').first().status == 'pago'


def test_pedido_entregue_bloqueia_mudanca(app):
    from app.extensions import db
    c = _owner(app)
    _pedido(db, codigo='ST0005', status='entregue')
    c.post('/admin/loja-online/pedidos/ST0005/status',
           data={'novo_status': 'em_preparo'})
    with app.app_context():
        from app.models import PedidoOnline
        assert PedidoOnline.query.filter_by(codigo='ST0005').first().status == 'entregue'


def test_status_exige_owner(app):
    """Sem owner: 302 pra login admin (não muda nada)."""
    from app.extensions import db
    c = app.test_client()
    with app.app_context():
        _pedido(db, codigo='ST0006', status='pago')
    r = c.post('/admin/loja-online/pedidos/ST0006/status',
                data={'novo_status': 'em_preparo'}, follow_redirects=False)
    assert r.status_code in (302, 401, 403)
    with app.app_context():
        from app.models import PedidoOnline
        assert PedidoOnline.query.filter_by(codigo='ST0006').first().status == 'pago'


def test_email_a_caminho_falhando_nao_quebra_status(app):
    """E-mail falhar NÃO impede a mudança de status (best-effort)."""
    from app.extensions import db
    c = _owner(app)
    _pedido(db, codigo='ST0007', status='pago')
    with patch('app.services.email.disponivel', return_value=True), \
         patch('app.services.email.enviar_pedido_a_caminho',
               side_effect=RuntimeError('postmark down')):
        r = c.post('/admin/loja-online/pedidos/ST0007/status',
                    data={'novo_status': 'a_caminho'})
    assert r.status_code == 302
    with app.app_context():
        from app.models import PedidoOnline
        assert PedidoOnline.query.filter_by(codigo='ST0007').first().status == 'a_caminho'

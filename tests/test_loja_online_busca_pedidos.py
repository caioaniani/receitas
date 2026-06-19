"""Busca incremental de pedidos do site (19/06/2026).

`/admin/loja-online/buscar-pedidos?q=` procura em TODOS os pedidos por nome,
telefone, e-mail ou código e devolve as linhas <tr> (mesmo partial da lista).
Mínimo 2 letras. Path separado de /pedidos/<codigo> pra não colidir.
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


def _pedido(db, codigo, nome, tel='', email='cli@x.com', status='pago'):
    from app.models import PedidoOnline
    p = PedidoOnline(
        codigo=codigo, nome_cliente=nome, email_cliente=email,
        telefone_cliente=tel, modo_entrega='retirada', status=status,
        subtotal=Decimal('10'), frete_valor=Decimal('0'),
        valor_total=Decimal('10'))
    db.session.add(p)
    db.session.commit()
    return p


def test_busca_por_nome(app):
    from app.extensions import db
    with app.app_context():
        _pedido(db, 'AAA111', 'Ana Carolina Reis', tel='11988887777')
        _pedido(db, 'BBB222', 'João Pereira', tel='11977776666')
    c = _owner(app)
    r = c.get('/admin/loja-online/buscar-pedidos?q=carolina')
    assert r.status_code == 200
    assert b'AAA111' in r.data
    assert b'BBB222' not in r.data


def test_busca_por_telefone(app):
    from app.extensions import db
    with app.app_context():
        _pedido(db, 'AAA111', 'Ana', tel='11988887777')
        _pedido(db, 'BBB222', 'Joao', tel='11977776666')
    c = _owner(app)
    r = c.get('/admin/loja-online/buscar-pedidos?q=98888')
    assert b'AAA111' in r.data
    assert b'BBB222' not in r.data


def test_busca_por_codigo(app):
    from app.extensions import db
    with app.app_context():
        _pedido(db, '3CF0B2EE', 'Ana', tel='1199')
        _pedido(db, 'ZZZ999', 'Joao', tel='1188')
    c = _owner(app)
    r = c.get('/admin/loja-online/buscar-pedidos?q=3cf0')  # case-insensitive
    assert b'3CF0B2EE' in r.data
    assert b'ZZZ999' not in r.data


def test_busca_curta_devolve_vazio(app):
    from app.extensions import db
    with app.app_context():
        _pedido(db, 'AAA111', 'Ana', tel='1199')
    c = _owner(app)
    r = c.get('/admin/loja-online/buscar-pedidos?q=a')  # < 2 chars
    assert r.status_code == 200
    assert r.data.strip() == b''


def test_busca_sem_resultado(app):
    from app.extensions import db
    with app.app_context():
        _pedido(db, 'AAA111', 'Ana', tel='1199')
    c = _owner(app)
    r = c.get('/admin/loja-online/buscar-pedidos?q=inexistente')
    assert r.status_code == 200
    assert r.data.strip() == b''


def test_busca_nao_colide_com_detalhe(app):
    """A rota de busca não pode ser engolida por /pedidos/<codigo>."""
    from app.extensions import db
    with app.app_context():
        _pedido(db, 'AAA111', 'Ana Teste', tel='1199')
    c = _owner(app)
    r = c.get('/admin/loja-online/buscar-pedidos?q=Ana')
    assert r.status_code == 200
    assert b'AAA111' in r.data


def test_pagina_pedidos_tem_campo_busca(app):
    from app.extensions import db
    with app.app_context():
        _pedido(db, 'AAA111', 'Ana', tel='1199')
    c = _owner(app)
    r = c.get('/admin/loja-online/pedidos')
    assert r.status_code == 200
    assert b'busca-pedido' in r.data

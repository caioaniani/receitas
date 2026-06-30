"""Painel de vendas online (/admin/loja-online/vendas) + resumo_clientes +
instrumentação do funil no GA4. (30/06/2026)
"""
import os
from datetime import timedelta
from decimal import Decimal

from app.extensions import db
from app.utils import agora, hoje


def _login(app, owner):
    from app.models import Usuario
    login = 'dono' if owner else 'adm'
    u = Usuario(nome='X', login=login, papel='admin', is_owner=owner)
    u.set_senha('12345678')
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': login, 'senha': '12345678'},
           follow_redirects=True)
    return c


def _cliente(email):
    from app.models import Cliente
    cl = Cliente(nome='C', email=email)
    db.session.add(cl)
    db.session.commit()
    return cl


def _produto_box():
    from app.models import Produto
    p = Produto(nome='Box Mimo', categoria='Cestas',
                preco_site=Decimal('50'), ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


def _pedido_pago(cliente_id, dias_atras, produto_id=None):
    from app.models import PedidoOnline, PedidoOnlineItem
    p = PedidoOnline(nome_cliente='X', email_cliente=f'c{cliente_id}@x.com',
                     telefone_cliente='11999', modo_entrega='retirada',
                     status='pago', subtotal=Decimal('50'),
                     valor_total=Decimal('50'), cliente_id=cliente_id,
                     pago_em=agora() - timedelta(days=dias_atras))
    p.itens.append(PedidoOnlineItem(kind='produto', nome='Box Mimo',
                   produto_id=produto_id, preco_unitario=Decimal('50'),
                   quantidade=1, subtotal=Decimal('50')))
    db.session.add(p)
    db.session.commit()
    return p


def test_resumo_clientes_novos_vs_recorrentes(app):
    from app.services.loja_online_vendas import resumo_clientes
    with app.app_context():
        a = _cliente('a@x.com')   # recorrente (comprou ANTES e no período)
        b = _cliente('b@x.com')   # novo (primeira compra no período)
        _pedido_pago(a.id, dias_atras=60)   # antes do período
        _pedido_pago(a.id, dias_atras=5)    # no período
        _pedido_pago(b.id, dias_atras=3)    # no período
        r = resumo_clientes(hoje() - timedelta(days=29), hoje())
    assert r['total'] == 2
    assert r['novos'] == 1
    assert r['recorrentes'] == 1


def test_painel_vendas_owner_ve(app):
    c = _login(app, owner=True)
    assert c.get('/admin/loja-online/vendas').status_code == 200


def test_painel_vendas_nao_owner_403(app):
    c = _login(app, owner=False)
    assert c.get('/admin/loja-online/vendas').status_code in (302, 403)


def test_painel_vendas_mostra_metricas(app):
    c = _login(app, owner=True)
    with app.app_context():
        prod = _produto_box()
        cl = _cliente('m@x.com')
        _pedido_pago(cl.id, dias_atras=2, produto_id=prod.id)
    html = c.get('/admin/loja-online/vendas?dias=30').data
    assert b'Faturamento' in html
    assert b'Ticket' in html
    assert b'Box Mimo' in html          # top produtos
    assert b'recorrentes' in html


def test_ga4_eventos_instrumentados():
    """Os 3 eventos de funil estão disparando na loja (sem isso o GA4 só media
    visitas — não dava pra ver onde o cliente desiste)."""
    base = os.path.join(os.path.dirname(__file__), '..', 'app', 'static', 'loja')
    carrinho = open(os.path.join(base, 'carrinho.js'), encoding='utf-8').read()
    checkout = open(os.path.join(base, 'checkout.js'), encoding='utf-8').read()
    assert 'add_to_cart' in carrinho
    assert 'window.lojaGA' in carrinho
    assert 'begin_checkout' in checkout

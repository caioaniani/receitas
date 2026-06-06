"""Pedidos do site (VNDA) no card de cliente do CRM.

Cobre: a sincronizacao que popula o cache `PedidoSite` (com telefone vindo do
endereco de entrega do VNDA) e o card retornando esses pedidos casados por
telefone.
"""
import json
from datetime import date
from decimal import Decimal
from unittest.mock import patch


def test_sincronizar_periodo_cria_pedido_site(app):
    from app.extensions import db
    from app.models import PedidoSite
    from app.services import vnda_card
    from app.utils import telefone_chave

    fake_orders = [
        {
            'code': 'V-1',
            'status': 'paid',
            'client_name': 'João',
            'confirmed_at': '2026-06-01T10:00:00Z',
            'total': 50.0,
            'items': [{'product_name': 'Pão', 'quantity': 2, 'price': 25.0}],
            # phone inline → sem chamada extra à API (recipient_name = destinatario)
            'shipping_address': {'phone': '(11) 98888-7777', 'recipient_name': 'João'},
        },
        {
            'code': 'V-2',
            'status': 'canceled',  # cancelado deve ser pulado
            'client_name': 'Ana',
            'shipping_address': {'phone': '11955554444'},
            'items': [],
        },
    ]

    with app.app_context():
        with patch('app.services.vnda._buscar_pedidos_janela', return_value=fake_orders):
            r = vnda_card.sincronizar_periodo(date(2026, 5, 1), date(2026, 6, 2))

        assert r['sincronizados'] == 1  # V-2 cancelado nao conta
        ps = PedidoSite.query.get('V-1')
        assert ps is not None
        assert ps.telefone_chave == telefone_chave('(11) 98888-7777')
        assert ps.comprador == 'João'
        itens = json.loads(ps.itens_json)
        assert itens == [{'nome': 'Pão', 'qtd': 2, 'preco': 25.0}]
        # cancelado nao entrou no cache
        assert PedidoSite.query.get('V-2') is None
        db.session.remove()


def test_sincronizar_periodo_idempotente(app):
    """Rodar de novo o mesmo pedido atualiza (nao duplica)."""
    from app.models import PedidoSite
    from app.services import vnda_card

    order = {'code': 'V-9', 'status': 'paid', 'client_name': 'X',
             'total': 10.0, 'items': [],
             'shipping_address': {'phone': '11999990000'}}
    with app.app_context():
        with patch('app.services.vnda._buscar_pedidos_janela', return_value=[order]):
            vnda_card.sincronizar_periodo(date(2026, 5, 1), date(2026, 6, 1))
            order['total'] = 20.0
            vnda_card.sincronizar_periodo(date(2026, 5, 1), date(2026, 6, 1))
        assert PedidoSite.query.filter_by(code='V-9').count() == 1
        assert float(PedidoSite.query.get('V-9').total) == 20.0


def test_card_inclui_pedidos_site(app):
    from app.extensions import db
    from app.models import PedidoSite
    from app.utils import telefone_chave

    app.config['CHATWOOT_CARD_TOKEN'] = 'segredo'
    with app.app_context():
        db.session.add(PedidoSite(
            code='V-100',
            telefone='(11) 99999-8888',
            telefone_chave=telefone_chave('(11) 99999-8888'),
            comprador='Maria',
            destinatario='Maria',
            data_pedido=date(2026, 6, 1),
            total=Decimal('80.00'),
            status_vnda='paid',
            itens_json=json.dumps([{'nome': 'Bolo', 'qtd': 1, 'preco': 80.0}]),
        ))
        db.session.commit()

    client = app.test_client()
    # WhatsApp manda 55 + 9o digito; o card casa pela chave canonica
    r = client.get('/crm/card.json?phone=5511999998888&k=segredo')
    assert r.status_code == 200
    j = r.get_json()
    assert j['encontrado'] is True
    assert len(j['pedidos_site']) == 1
    p = j['pedidos_site'][0]
    assert p['code'] == 'V-100'
    assert p['total'] == 80.0
    assert p['destinatario'] == 'Maria'
    assert p['itens'][0]['nome'] == 'Bolo'


def test_card_sem_pedido_site_telefone_desconhecido(app):
    app.config['CHATWOOT_CARD_TOKEN'] = 'segredo'
    client = app.test_client()
    r = client.get('/crm/card.json?phone=5511900000000&k=segredo')
    assert r.status_code == 200
    j = r.get_json()
    assert j['pedidos_site'] == []

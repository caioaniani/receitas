"""A tela de Vendas (/pdv/api/vendas) agora devolve um bloco `site` com o
faturamento da loja propria (PedidoOnline). O front soma isso no total/ticket e
numa linha 'Site' do canal de venda. A fonte Seru (PDV) continua separada.

Regra do site (loja_online_vendas): so pedido PAGO e nao cancelado; faturamento
= subtotal (SEM frete); por data de venda (pago_em).
"""
from decimal import Decimal
from unittest.mock import patch

from app.extensions import db
from app.models import Cliente, PedidoOnline
from app.utils import agora, hoje


def _login(cliente, admin_user):
    cliente.post('/auth/login', data={'login': admin_user.login, 'senha': '123'})


def _pedido_site(codigo, subtotal, status='pago', pago=True):
    cli = Cliente(nome='C', email=f'{codigo.lower()}@x.com')
    db.session.add(cli)
    db.session.flush()
    sub = Decimal(str(subtotal))
    p = PedidoOnline(
        codigo=codigo, cliente_id=cli.id, nome_cliente='C',
        email_cliente=cli.email, modo_entrega='retirada', status=status,
        subtotal=sub, frete_valor=Decimal('8.00'),
        valor_total=sub + Decimal('8.00'),
        pago_em=(agora() if pago else None),
    )
    db.session.add(p)
    db.session.commit()
    return p


def test_api_vendas_inclui_bloco_site(app, admin_user):
    cliente = app.test_client()
    _login(cliente, admin_user)
    _pedido_site('SITE1', 50.0)
    hj = hoje().isoformat()
    with patch('app.services.seru.listar_pedidos_completo', return_value=[]):
        r = cliente.get(f'/pdv/api/vendas?inicio={hj}&fim={hj}')
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] is True
    assert j['total_valor'] == 0            # Seru vazio (site é separado)
    assert j['site']['n_pedidos'] == 1
    assert j['site']['total'] == 50.0       # subtotal, sem o frete de 8


def test_api_vendas_site_ignora_nao_pago_e_cancelado(app, admin_user):
    cliente = app.test_client()
    _login(cliente, admin_user)
    _pedido_site('SITE_CANC', 50.0, status='cancelado')          # cancelado
    _pedido_site('SITE_AGUARD', 30.0, status='aguardando_pagamento',
                 pago=False)                                      # não pago
    hj = hoje().isoformat()
    with patch('app.services.seru.listar_pedidos_completo', return_value=[]):
        r = cliente.get(f'/pdv/api/vendas?inicio={hj}&fim={hj}')
    j = r.get_json()
    assert j['site']['n_pedidos'] == 0
    assert j['site']['total'] == 0

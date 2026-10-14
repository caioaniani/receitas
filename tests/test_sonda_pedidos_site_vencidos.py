"""Sonda /api/claude/pedidos-site-vencidos (14/10/2026)."""
from datetime import timedelta
from decimal import Decimal

from app.extensions import db
from app.models import PainelPedidoStatus, PedidoOnline
from app.utils import hoje


def _pedido(codigo, status, dias):
    p = PedidoOnline(codigo=codigo, nome_cliente='C', email_cliente='c@x.test',
                     modo_entrega='agendada', status=status,
                     data_entrega=hoje() - timedelta(days=dias),
                     subtotal=Decimal('10'), frete_valor=Decimal('0'),
                     valor_total=Decimal('10'))
    db.session.add(p)
    return p


def test_lista_so_pagos_vencidos_com_status_do_painel(app):
    app.config['CLAUDE_API_TOKEN'] = 'tok'
    _pedido('VENC0001', 'pago', 2)
    _pedido('VENC0002', 'entregue', 2)
    _pedido('VENC0003', 'pago', 0)          # hoje: fora
    _pedido('VENC0004', 'cancelado', 3)
    db.session.add(PainelPedidoStatus(pedido_code='VENC0001', status='entregue'))
    db.session.commit()
    r = app.test_client().get('/api/claude/pedidos-site-vencidos',
                              headers={'Authorization': 'Bearer tok'})
    d = r.get_json()
    assert r.status_code == 200, d
    assert [p['codigo'] for p in d['pedidos']] == ['VENC0001']
    assert d['pedidos'][0]['painel']['status'] == 'entregue'
    assert d['pedidos'][0]['motivo_bloqueio_saida'] is None

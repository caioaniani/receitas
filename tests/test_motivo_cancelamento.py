"""Motivo do cancelamento de pedido do site (25/06/2026).

`PedidoOnline.motivo_cancelamento` registra POR QUE o pedido foi cancelado
(pix_expirado / reembolso / cancelado_admin), em vez de deduzir pelos
timestamps. A property `motivo_cancelamento_label` traduz o codigo pra UI e,
para pedidos cancelados ANTES desta coluna existir (motivo NULL), infere pelos
timestamps — era o que se fazia na mao (ex: o pedido ae16cfa4).

Os 3 caminhos que gravam o motivo sao travados nos testes de integracao:
- pix_expirado: test_loja_estoque_reserva::test_liberar_expirados_cancela_...
- reembolso:    test_loja_pagamento_pix_cartao::test_reembolsar_pedido_estorna
- cancelado_admin: test_loja_pedidos_admin::test_cancelar_pedido
"""
from datetime import datetime


def _pedido(**kw):
    from app.models import PedidoOnline
    return PedidoOnline(**kw)


def test_label_traduz_codigo_persistido():
    p = _pedido(status='cancelado', motivo_cancelamento='pix_expirado')
    assert p.motivo_cancelamento_label == 'Pix não pago (reserva expirou)'
    p.motivo_cancelamento = 'reembolso'
    assert p.motivo_cancelamento_label == 'Reembolsado pelo admin'
    p.motivo_cancelamento = 'cancelado_admin'
    assert p.motivo_cancelamento_label == 'Cancelado manualmente (admin)'


def test_label_codigo_desconhecido_volta_o_proprio_codigo():
    p = _pedido(status='cancelado', motivo_cancelamento='algo_novo')
    assert p.motivo_cancelamento_label == 'algo_novo'


def test_label_infere_pix_nao_pago_quando_motivo_nulo_e_sem_pagamento():
    # Legado (ex: ae16cfa4): cancelado antes da coluna, nunca pago.
    p = _pedido(status='cancelado', motivo_cancelamento=None, pago_em=None)
    assert p.motivo_cancelamento_label == 'Pix não pago (inferido)'


def test_label_infere_cancelado_pos_pagamento_quando_havia_pago_em():
    p = _pedido(status='cancelado', motivo_cancelamento=None,
                pago_em=datetime(2026, 6, 1, 10, 0))
    assert p.motivo_cancelamento_label == 'Cancelado após pagamento (inferido)'


def test_label_none_quando_pedido_nao_esta_cancelado():
    p = _pedido(status='pago', motivo_cancelamento=None)
    assert p.motivo_cancelamento_label is None

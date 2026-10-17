"""Pix vencido no PRÓPRIO Pagar.me que ele se recusa a cancelar não segura o
pedido (17/10/2026, caso 0C70AA31 — Raphaela: Pix de 30 min gerado 20:48,
nunca pago; ela refez o pedido no cartão às 21:00 e o 0C70AA31 ficou 12 h
"aguardando pagamento": o gateway seguia `pending` com `expires_at` vencido e
o cron adiava a reserva a cada 5 min sem registrar o motivo).

Pagar.me sempre mockado. Reusa os helpers dos testes da auditoria do checkout.
"""
from datetime import timedelta
from unittest.mock import patch

from app.utils import agora
from tests.test_checkout_auditoria_graves import _expirado, _pedido_pag, _produto

RECUSA = {'ok': False, 'incerto': True, 'rede': False,
          'erro': 'HTTP 412: {"message":"Charge can not be canceled"}'}
REDE = {'ok': False, 'incerto': True, 'rede': True, 'erro': 'timeout'}


def _pendente(vencido_ha_min):
    return {'ok': True, 'status': 'pending', 'pago': False,
            'charge_status': 'pending',
            'pix_expires_at': agora() - timedelta(minutes=vencido_ha_min)}


def test_cron_cancela_pix_vencido_no_gateway_mesmo_com_cancelamento_recusado(app):
    from app.extensions import db
    from app.services import loja_estoque_reserva
    prod = _produto(db)
    ped, pag = _pedido_pag(db, prod, metodo='pix')
    _expirado(db, ped, pag)
    with patch('app.services.pagarme.consultar_order', return_value=_pendente(120)), \
            patch('app.services.pagarme.cancelar_cobranca_pendente', return_value=RECUSA):
        assert loja_estoque_reserva.liberar_expirados() == [ped.codigo]
    db.session.refresh(ped)
    db.session.refresh(pag)
    assert ped.status == 'cancelado' and ped.motivo_cancelamento == 'pix_expirado'
    assert pag.status == 'falhou'
    assert 'expirado' in pag.erro and 'HTTP 412' in pag.erro


def test_cron_adia_e_registra_quando_a_recusa_vem_sem_vencimento_do_gateway(app):
    """Sem `pix_expires_at` (gateway não informou) a regra antiga vale:
    adia — mas agora o motivo fica gravado na tentativa, não só no log."""
    from app.extensions import db
    from app.services import loja_estoque_reserva
    prod = _produto(db)
    ped, pag = _pedido_pag(db, prod, metodo='pix')
    _expirado(db, ped, pag)
    sem_data = {'ok': True, 'status': 'pending', 'pago': False,
                'charge_status': 'pending', 'pix_expires_at': None}
    with patch('app.services.pagarme.consultar_order', return_value=sem_data), \
            patch('app.services.pagarme.cancelar_cobranca_pendente', return_value=RECUSA):
        assert loja_estoque_reserva.liberar_expirados() == []
    db.session.refresh(ped)
    db.session.refresh(pag)
    assert ped.status == 'aguardando_pagamento' and pag.status == 'pendente'
    assert 'HTTP 412' in pag.erro


def test_cron_adia_dentro_da_folga_do_vencimento(app):
    from app.extensions import db
    from app.services import loja_estoque_reserva
    prod = _produto(db)
    ped, pag = _pedido_pag(db, prod, metodo='pix')
    _expirado(db, ped, pag)
    with patch('app.services.pagarme.consultar_order', return_value=_pendente(10)), \
            patch('app.services.pagarme.cancelar_cobranca_pendente', return_value=RECUSA):
        assert loja_estoque_reserva.liberar_expirados() == []
    db.session.refresh(ped)
    assert ped.status == 'aguardando_pagamento'


def test_cron_adia_quando_o_cancelamento_falha_por_rede(app):
    """Rede fora não é recusa: o QR pode estar morto, mas sem resposta do
    gateway o sistema não decide — tenta no ciclo seguinte."""
    from app.extensions import db
    from app.services import loja_estoque_reserva
    prod = _produto(db)
    ped, pag = _pedido_pag(db, prod, metodo='pix')
    _expirado(db, ped, pag)
    with patch('app.services.pagarme.consultar_order', return_value=_pendente(120)), \
            patch('app.services.pagarme.cancelar_cobranca_pendente', return_value=REDE):
        assert loja_estoque_reserva.liberar_expirados() == []
    db.session.refresh(ped)
    db.session.refresh(pag)
    assert ped.status == 'aguardando_pagamento' and pag.status == 'pendente'
    assert 'timeout' in pag.erro


def test_nova_cobranca_passa_quando_o_pix_anterior_venceu_no_gateway(app):
    """Cliente volta à página de pagamento depois do QR vencido: a tentativa
    antiga, que o gateway recusa cancelar mas diz vencida, não barra a nova."""
    from app.extensions import db
    from app.services import loja_pagamento
    prod = _produto(db)
    ped, pag = _pedido_pag(db, prod, metodo='pix')
    with patch('app.services.pagarme.consultar_order', return_value=_pendente(120)), \
            patch('app.services.pagarme.cancelar_cobranca_pendente', return_value=RECUSA):
        assert loja_pagamento._resolver_tentativas_pendentes(ped) is None
    db.session.refresh(pag)
    assert pag.status == 'falhou' and 'substituído' in pag.erro


def test_nova_cobranca_segue_barrada_com_pix_ainda_dentro_da_folga(app):
    from app.extensions import db
    from app.services import loja_pagamento
    prod = _produto(db)
    ped, pag = _pedido_pag(db, prod, metodo='pix')
    with patch('app.services.pagarme.consultar_order', return_value=_pendente(5)), \
            patch('app.services.pagarme.cancelar_cobranca_pendente', return_value=RECUSA):
        assert (loja_pagamento._resolver_tentativas_pendentes(ped)
                == loja_pagamento._MSG_PIX_ANTERIOR_ATIVO)


def test_consultar_order_le_o_vencimento_do_pix_em_brt(app):
    from unittest.mock import MagicMock

    from app.services import pagarme
    corpo = {'status': 'pending', 'charges': [{
        'id': 'ch_1', 'status': 'pending', 'payment_method': 'pix',
        'last_transaction': {'status': 'waiting_payment', 'transaction_type': 'pix',
                             'expires_at': '2026-10-17T00:18:36Z'}}]}
    resp = MagicMock(status_code=200)
    resp.json.return_value = corpo
    with patch.object(pagarme, 'disponivel', return_value=True), \
            patch('app.services.pagarme.requests.get', return_value=resp):
        out = pagarme.consultar_order('or_1')
    assert out['pix_expires_at'].isoformat() == '2026-10-16T21:18:36'
    # Cartão não tem vencimento de QR; data ilegível não derruba a consulta.
    corpo['charges'][0]['last_transaction'] = {'transaction_type': 'credit_card'}
    with patch.object(pagarme, 'disponivel', return_value=True), \
            patch('app.services.pagarme.requests.get', return_value=resp):
        assert pagarme.consultar_order('or_1')['pix_expires_at'] is None
    corpo['charges'][0]['last_transaction'] = {'transaction_type': 'pix', 'expires_at': 'x'}
    with patch.object(pagarme, 'disponivel', return_value=True), \
            patch('app.services.pagarme.requests.get', return_value=resp):
        assert pagarme.consultar_order('or_1')['pix_expires_at'] is None


def test_cancelar_distingue_rede_de_recusa(app):
    from unittest.mock import MagicMock

    import requests as req

    from app.services import pagarme
    with patch.object(pagarme, 'disponivel', return_value=True), \
            patch('app.services.pagarme.requests.delete',
                  side_effect=req.ConnectionError('x')):
        assert pagarme.cancelar_cobranca_pendente('ch_1')['rede'] is True
    r = MagicMock(status_code=412, text='{"message":"nope"}')
    with patch.object(pagarme, 'disponivel', return_value=True), \
            patch('app.services.pagarme.requests.delete', return_value=r):
        out = pagarme.cancelar_cobranca_pendente('ch_1')
    assert out['rede'] is False and out['erro'].startswith('HTTP 412')
    r = MagicMock(status_code=503, text='')
    with patch.object(pagarme, 'disponivel', return_value=True), \
            patch('app.services.pagarme.requests.delete', return_value=r):
        assert pagarme.cancelar_cobranca_pendente('ch_1')['rede'] is True


def test_sonda_pedidos_site_expoe_gateway_so_com_codigo(app):
    from app.extensions import db
    prod = _produto(db)
    ped, pag = _pedido_pag(db, prod, metodo='pix')
    app.config['CLAUDE_API_TOKEN'] = 'tok'
    client = app.test_client()
    h = {'Authorization': 'Bearer tok'}
    with patch('app.services.pagarme.consultar_order', return_value=_pendente(1)) as co, \
            patch('app.services.pagarme.consultar_charge_bruto',
                  return_value={'ok': True, 'status': 'pending'}) as cc:
        sem = client.get('/api/claude/pedidos-site?dias=1&gateway=1', headers=h).get_json()
        com = client.get(f'/api/claude/pedidos-site?codigo={ped.codigo}&gateway=1',
                         headers=h).get_json()
    assert 'gateway' not in sem['pedidos'][0]['pagamentos'][0]
    assert co.call_count == 1 and cc.call_count == 1
    p = com['pedidos'][0]
    assert p['codigo'] == ped.codigo
    assert p['pagamentos'][0]['gateway']['charge']['status'] == 'pending'
    assert 'reserva_expira_em' in p and 'pix_expira_em' in p['pagamentos'][0]

"""Purchase server-side (GA4 Measurement Protocol + Meta CAPI) — 13/07/2026.

Cobre: extração do client_id do cookie _ga, payloads (dedupe por
transaction_id/event_id, hashes da Meta), no-op sem envs, kill-switch e o
gancho no webhook do Pagar.me. Requests SEMPRE mockado — teste nunca sai
pra internet.
"""
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.extensions import db
from app.services import analytics_server


def _pedido(codigo='ANA001', ga_client_id=None):
    from app.models import Cliente, PedidoOnline, PedidoOnlineItem
    cli = Cliente(nome='Maria', email=f'{codigo.lower()}@x.com')
    db.session.add(cli)
    db.session.flush()
    p = PedidoOnline(codigo=codigo, cliente_id=cli.id,
                     nome_cliente='Maria', email_cliente=cli.email,
                     telefone_cliente='(11) 98888-7777',
                     modo_entrega='retirada', status='pago',
                     subtotal=Decimal('40'), frete_valor=Decimal('10'),
                     valor_total=Decimal('50'),
                     pago_em=datetime(2026, 7, 13, 15, 0),
                     ga_client_id=ga_client_id)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoOnlineItem(pedido_id=p.id, kind='produto',
                                    produto_id=None, nome='Family Box',
                                    quantidade=2,
                                    preco_unitario=Decimal('20')))
    db.session.commit()
    return p


def test_ga_client_id_do_cookie():
    f = analytics_server.ga_client_id_do_cookie
    assert f('GA1.1.123456789.1720000000') == '123456789.1720000000'
    assert f('GA1.2.111.222') == '111.222'
    assert f('lixo') is None
    assert f('GA1.1.abc.def') is None
    assert f('') is None
    assert f(None) is None


def test_payload_ga4_usa_client_id_capturado_e_fallback(app):
    with app.app_context():
        p = _pedido('ANA002', ga_client_id='123.456')
        pl = analytics_server._payload_ga4(p)
        assert pl['client_id'] == '123.456'
        ev = pl['events'][0]
        assert ev['name'] == 'purchase'
        assert ev['params']['transaction_id'] == 'ANA002'
        assert ev['params']['value'] == 50.0
        assert ev['params']['shipping'] == 10.0
        assert ev['params']['items'][0]['quantity'] == 2

        p2 = _pedido('ANA003')  # sem client_id → sintético (sem browser event)
        assert analytics_server._payload_ga4(p2)['client_id'] == f'srv.{p2.id}'


def test_payload_meta_hasheia_pii_e_deduplica_por_event_id(app):
    import hashlib
    with app.app_context():
        p = _pedido('ANA004')
        pl = analytics_server._payload_meta(p)
        ev = pl['data'][0]
        assert ev['event_name'] == 'Purchase'
        assert ev['event_id'] == 'ANA004'  # mesmo eventID do fbq do navegador
        assert ev['custom_data'] == {'value': 50.0, 'currency': 'BRL'}
        # PII NUNCA em claro — sha256 do e-mail minúsculo e do fone com 55.
        assert ev['user_data']['em'] == [
            hashlib.sha256(b'ana004@x.com').hexdigest()]
        assert ev['user_data']['ph'] == [
            hashlib.sha256(b'5511988887777').hexdigest()]
        raw = str(pl)
        assert 'ana004@x.com' not in raw and '11988887777' not in raw


def test_sem_envs_e_noop_sem_request(app):
    with app.app_context():
        app.config['GA4_ID'] = ''
        app.config['GA4_API_SECRET'] = ''
        app.config['META_PIXEL_ID'] = ''
        app.config['META_CAPI_TOKEN'] = ''
        p = _pedido('ANA005')
        with patch.object(analytics_server.requests, 'post') as post:
            out = analytics_server.reportar_purchase(p.id)
        post.assert_not_called()
        assert out == {'ga4': False, 'meta': False}


def test_com_envs_envia_ga4_e_meta(app):
    with app.app_context():
        app.config['GA4_ID'] = 'G-TESTE'
        app.config['GA4_API_SECRET'] = 'segredo'
        app.config['META_PIXEL_ID'] = '123456789012345'
        app.config['META_CAPI_TOKEN'] = 'tok'
        p = _pedido('ANA006', ga_client_id='9.9')
        with patch.object(analytics_server.requests, 'post') as post:
            post.return_value.status_code = 200
            out = analytics_server.reportar_purchase(p.id)
        assert out == {'ga4': True, 'meta': True}
        assert post.call_count == 2
        url_ga4 = post.call_args_list[0][0][0]
        assert 'google-analytics.com/mp/collect' in url_ga4
        assert post.call_args_list[0][1]['params']['measurement_id'] == 'G-TESTE'
        url_meta = post.call_args_list[1][0][0]
        assert '123456789012345/events' in url_meta


def test_kill_switch_e_pedido_nao_pago(app):
    with app.app_context():
        app.config['GA4_ID'] = 'G-TESTE'
        app.config['GA4_API_SECRET'] = 'segredo'
        p = _pedido('ANA007')
        app.config['ANALYTICS_SERVER'] = '0'
        with patch.object(analytics_server.requests, 'post') as post:
            assert analytics_server.reportar_purchase(p.id) == {
                'ga4': False, 'meta': False}
        post.assert_not_called()
        app.config['ANALYTICS_SERVER'] = '1'
        p.pago_em = None
        db.session.commit()
        with patch.object(analytics_server.requests, 'post') as post:
            assert analytics_server.reportar_purchase(p.id) == {
                'ga4': False, 'meta': False}
        post.assert_not_called()


def test_erro_de_rede_nao_propaga(app):
    with app.app_context():
        app.config['GA4_ID'] = 'G-TESTE'
        app.config['GA4_API_SECRET'] = 'segredo'
        app.config['META_PIXEL_ID'] = '1'
        app.config['META_CAPI_TOKEN'] = 't'
        p = _pedido('ANA008')
        with patch.object(analytics_server.requests, 'post',
                          side_effect=Exception('boom')):
            out = analytics_server.reportar_purchase(p.id)  # não levanta
        assert out == {'ga4': False, 'meta': False}


def test_webhook_pago_dispara_reporte(app):
    """O gancho no processar_webhook chama o reporte APÓS o commit do pago
    (mesmo padrão da NF) — e só quando o status realmente mudou."""
    from app.models import PagamentoOnline
    from app.services import loja_pagamento
    with app.app_context():
        p = _pedido('ANA009')
        p.status = 'aguardando_pagamento'
        p.pago_em = None
        pag = PagamentoOnline(pedido_id=p.id, metodo='pix',
                              valor=Decimal('50'), status='pendente',
                              pagarme_order_id='or_x9')
        db.session.add(pag)
        db.session.commit()
        evento = {'id': 'evt-ana-1', 'type': 'order.paid',
                  'data': {'id': 'or_x9'}}
        with patch.object(loja_pagamento, '_emitir_nf_e_enviar'), \
                patch('app.services.analytics_server.reportar_purchase_async') as rep:
            out = loja_pagamento.processar_webhook(evento)
        assert out.get('pago') is True and out.get('mudou') is True
        rep.assert_called_once_with(p.id)
        # Reentrega do webhook (mesmo evento) não re-reporta.
        with patch.object(loja_pagamento, '_emitir_nf_e_enviar'), \
                patch('app.services.analytics_server.reportar_purchase_async') as rep2:
            loja_pagamento.processar_webhook(evento)
        rep2.assert_not_called()


@pytest.mark.loja_host
def test_checkout_captura_cookie_ga(app):
    """O POST do checkout grava o client_id do cookie _ga no pedido."""
    import json as _json

    from app.models import PedidoOnline, Usuario
    from app.services import loja_checkout
    from tests.test_loja_checkout import _loja, _produto_pub

    # Cliente logado como admin (mesma receita do test_loja_checkout — o
    # gate de host/visibilidade não é o assunto deste teste).
    u = Usuario(nome='Admin', login='adm-ga', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    prod = _produto_pub(db, nome='Box GA')
    loja = _loja(db)
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    c.set_cookie('_ga', 'GA1.1.555444333.1720900000')
    data = loja_checkout.datas_disponiveis('retirada')[-1].isoformat()
    r = c.post('/loja/checkout', data={
        'nome': 'Maria', 'sobrenome': 'Silva', 'email': 'ga@x.com',
        'telefone': '11988887777', 'cpf': '52998224725',
        'aceite_lgpd': '1', 'modo_entrega': 'retirada',
        'loja_id': str(loja.id), 'data_entrega': data,
        'janela_entrega': '08:00\u201309:00',
        'itens_json': _json.dumps([{'kind': 'produto', 'id': prod.id,
                                    'qtd': 1}]),
    }, follow_redirects=False)
    assert r.status_code in (302, 303)
    ped = PedidoOnline.query.filter_by(email_cliente='ga@x.com').first()
    assert ped is not None
    assert ped.ga_client_id == '555444333.1720900000'

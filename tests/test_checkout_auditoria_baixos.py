"""Regressão dos defeitos de severidade baixa da auditoria do checkout
(27-28/09/2026). Núcleo (checkout, pagamento, e-mails, admin); frete, CNPJ e
kits têm os próprios arquivos."""
import logging
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.loja_host


def _produto(db, nome='Box Mimo', preco=20.0, **kw):
    from app.models import Produto
    p = Produto(nome=nome, categoria='Cestas', preco_site=preco,
                imagem_dropbox_url='https://x/p.jpg', ativo=True, **kw)
    db.session.add(p)
    db.session.commit()
    return p


def _pedido(db, prod, *, status='aguardando_pagamento', qtd=1, **kw):
    from app.models import PedidoOnline, PedidoOnlineItem
    p = PedidoOnline(nome_cliente='Maria', email_cliente='m@x.com',
                     telefone_cliente='11999998888',
                     modo_entrega=kw.pop('modo', 'retirada'),
                     status=status, frete_valor=kw.pop('frete', Decimal('0')),
                     subtotal=Decimal('0'), valor_total=Decimal('0'), **kw)
    db.session.add(p)
    db.session.flush()
    p.itens.append(PedidoOnlineItem(
        kind='produto', produto_id=prod.id, nome=prod.nome,
        preco_unitario=Decimal(str(prod.preco_site)), quantidade=qtd,
        subtotal=Decimal(str(prod.preco_site)) * qtd))
    p.recalcular_total()
    db.session.commit()
    return p


# ── 39. Log do Pagar.me sem dados pessoais ──────────────────────────────

class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = ''

    def json(self):
        return self._body


def test_log_do_cartao_recusado_nao_leva_cpf_nem_titular(app, caplog):
    from app.extensions import db
    from app.services import pagarme
    app.config['PAGARME_API_KEY'] = 'sk_test_abc'
    ped = _pedido(db, _produto(db))
    body = {
        'id': 'or_1',
        'customer': {'name': 'Maria Souza', 'document': '52998224725',
                     'email': 'maria@x.com'},
        'charges': [{'id': 'ch_1', 'status': 'failed', 'last_transaction': {
            'status': 'not_authorized', 'acquirer_return_code': '1000',
            'acquirer_message': 'Não autorizada',
            'card': {'holder_name': 'MARIA SOUZA', 'last_four_digits': '1234'},
        }}],
    }
    caplog.set_level(logging.WARNING)
    with patch('app.services.pagarme.requests.post',
               return_value=_Resp(200, body)):
        res = pagarme.criar_pedido_cartao(ped, 'tok')
    assert res['ok'] is False
    texto = caplog.text
    assert 'pagarme cartao recusado' in texto
    assert '52998224725' not in texto
    assert 'MARIA SOUZA' not in texto and 'maria@x.com' not in texto
    assert 'ch_1' in texto and 'retorno=1000' in texto


def test_log_de_http_de_erro_mostra_so_os_campos(app, caplog):
    from app.extensions import db
    from app.services import pagarme
    app.config['PAGARME_API_KEY'] = 'sk_test_abc'
    ped = _pedido(db, _produto(db))
    body = {'message': 'The request is invalid.',
            'errors': {'customer.document': ['invalid']},
            'request': {'customer': {'document': '52998224725'}}}
    caplog.set_level(logging.WARNING)
    with patch('app.services.pagarme.requests.post',
               return_value=_Resp(422, body)):
        res = pagarme.criar_pedido_pix(ped)
    assert res['ok'] is False
    assert 'customer.document' in caplog.text
    assert '52998224725' not in caplog.text


# ── 40. Analytics: purchase só de venda, 1x, com horário certo ──────────

def test_divulgacao_nao_dispara_purchase_em_nenhum_status(app, monkeypatch):
    from app.extensions import db
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    ped = _pedido(db, _produto(db), status='divulgacao', divulgacao=True)
    for status in ('divulgacao', 'entregue'):
        ped.status = status
        db.session.commit()
        h = app.test_client().get(f'/loja/pedido/{ped.codigo}').get_data(as_text=True)
        assert 'id="ga-purchase"' not in h, status


def test_purchase_so_nas_primeiras_24h_do_pagamento(app, monkeypatch):
    from app.extensions import db
    from app.utils import agora
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    ped = _pedido(db, _produto(db), status='pago', pago_em=agora())
    h = app.test_client().get(f'/loja/pedido/{ped.codigo}').get_data(as_text=True)
    assert 'id="ga-purchase"' in h and 'localStorage' in h
    ped.pago_em = agora() - timedelta(days=2)
    db.session.commit()
    h = app.test_client().get(f'/loja/pedido/{ped.codigo}').get_data(as_text=True)
    assert 'id="ga-purchase"' not in h


def test_event_time_da_meta_usa_o_fuso_de_brasilia():
    from datetime import datetime, timezone

    from app.services.analytics_server import _epoch_brt
    # 10:00 em Brasília = 13:00 UTC.
    esperado = int(datetime(2026, 9, 28, 13, 0, tzinfo=timezone.utc).timestamp())
    assert _epoch_brt(datetime(2026, 9, 28, 10, 0)) == esperado

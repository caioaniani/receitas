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


# ── 46/47. Express: re-render e prazo gravado ───────────────────────────

def test_rerender_com_express_fechado_volta_para_agendada(app, monkeypatch):
    import re

    from app.extensions import db
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    app.config['WTF_CSRF_ENABLED'] = False
    prod = _produto(db)
    c = app.test_client()
    with c.session_transaction() as s:
        s['carrinho'] = [{'kind': 'produto', 'id': prod.id, 'qtd': 1}]
    with patch('app.services.loja_checkout.express_disponivel',
               return_value=False):
        r = c.post('/loja/checkout', data={'nome': 'Maria Silva',
                                           'email': 'm@x.com',
                                           'modo_entrega': 'express'})
    h = r.get_data(as_text=True)
    assert r.status_code == 400
    radios = re.findall(r'<input type="radio" name="modo_entrega"[^>]*>', h)
    express = [x for x in radios if 'value="express"' in x][0]
    agendada = [x for x in radios if 'value="agendada"' in x][0]
    assert 'checked' not in express and 'disabled' in express
    assert 'checked' in agendada


def test_confirmacao_do_express_usa_o_prazo_gravado(app, monkeypatch):
    from app.extensions import db
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    ped = _pedido(db, _produto(db), status='pago', modo='express',
                  janela_entrega='em até 2h', endereco_entrega='Rua A, 10')
    h = app.test_client().get(f'/loja/pedido/{ped.codigo}').get_data(as_text=True)
    assert 'Express — em até 2h' in h and 'em até 1h' not in h


# ── 35. "Trocar pra dd/mm" só sugere data que o checkout aceita ─────────

def _disp(app, data, prod_id, **extra):
    return app.test_client().post(
        '/loja/api/disponibilidade-checkout',
        json={'data': data.isoformat(),
              'itens': [{'kind': 'produto', 'id': prod_id}], **extra}).get_json()


def test_sugestao_pula_dia_fechado(app):
    from app.extensions import db
    from app.services import loja_data_especial, loja_plano_dia
    from app.utils import hoje
    p = _produto(db)
    d1, d2, d3 = (hoje() + timedelta(days=i) for i in (1, 2, 3))
    loja_plano_dia.definir('produto', p.id, hoje(), 0)   # hoje pode ter janela
    loja_plano_dia.definir('produto', p.id, d1, 0)
    loja_data_especial.definir(d2, '', rotulo='Natal')
    assert _disp(app, d1, p.id)['proxima_disponivel'] == d3.isoformat()


def test_sugestao_respeita_o_d2_da_encomenda(app):
    from app.extensions import db
    from app.services import loja_plano_dia
    from app.utils import hoje
    p = _produto(db, sob_encomenda=True)
    d2, d3 = hoje() + timedelta(days=2), hoje() + timedelta(days=3)
    loja_plano_dia.definir('produto', p.id, d2, 0)
    assert _disp(app, d2, p.id)['proxima_disponivel'] == d3.isoformat()


def test_sugestao_nao_passa_do_fim_do_calendario(app):
    from app.extensions import db
    from app.services import loja_plano_dia
    from app.utils import hoje
    p = _produto(db)
    for i in range(0, 20):
        loja_plano_dia.definir('produto', p.id, hoje() + timedelta(days=i), 0)
    loja_plano_dia.definir('produto', p.id, hoje() + timedelta(days=20), 5)
    assert _disp(app, hoje() + timedelta(days=1), p.id)['proxima_disponivel'] is None


def test_item_bloqueado_na_data_especial_aparece_na_conferencia(app):
    from app.extensions import db
    from app.services import loja_data_especial, loja_plano_dia
    from app.utils import hoje
    p = _produto(db, nome='Caixa de Mini')
    d2, d3 = hoje() + timedelta(days=2), hoje() + timedelta(days=3)
    for i in (0, 1):   # hoje/amanhã sem saldo: a sugestão cai depois do bloqueio
        loja_plano_dia.definir('produto', p.id, hoje() + timedelta(days=i), 0)
    loja_data_especial.definir(d2, '06:00-10:00', rotulo='Dia dos Pais',
                               bloquear_itens='Caixa de Mini')
    j = _disp(app, d2, p.id)
    assert [e['nome'] for e in j['esgotados']] == ['Caixa de Mini']
    assert j['esgotados'][0]['bloqueado'] is True
    assert j['proxima_disponivel'] == d3.isoformat()


# ── 38. Pix gerado tarde: a reserva acompanha a validade do QR ──────────

def test_pix_gerado_tarde_estende_a_reserva_ate_o_fim_do_qr(app):
    from app.extensions import db
    from app.services import loja_pagamento
    from app.utils import agora
    ped = _pedido(db, _produto(db))
    ped.reserva_expira_em = agora() + timedelta(minutes=10)
    db.session.commit()
    fim_qr = agora() + timedelta(minutes=30)
    with patch('app.services.pagarme.criar_pedido_pix', return_value={
            'ok': True, 'order_id': 'or_1', 'charge_id': 'ch_1',
            'qr_code': 'qr', 'qr_code_url': 'u', 'expira_em': fim_qr}):
        pag, erros = loja_pagamento.iniciar_pix(ped)
    assert pag and not erros
    db.session.refresh(ped)
    assert ped.reserva_expira_em == fim_qr + timedelta(minutes=5)


def test_pix_que_falha_nao_estende_a_reserva(app):
    from app.extensions import db
    from app.services import loja_pagamento
    from app.utils import agora
    ped = _pedido(db, _produto(db))
    vence = agora() + timedelta(minutes=10)
    ped.reserva_expira_em = vence
    db.session.commit()
    with patch('app.services.pagarme.criar_pedido_pix',
               return_value={'ok': False, 'erro': 'x'}):
        loja_pagamento.iniciar_pix(ped)
    db.session.refresh(ped)
    assert ped.reserva_expira_em == vence


def test_pix_do_kit_estende_o_prazo_da_compra(app, owner_user, loja):
    from app.extensions import db
    from app.services import loja_pagamento
    from app.utils import agora
    from tests.test_kits_pagamento import compra as _compra_fixture
    compra = _compra_fixture.__wrapped__(app, owner_user, loja)
    compra.expira_em = agora() + timedelta(minutes=8)
    db.session.commit()
    fim_qr = agora() + timedelta(minutes=30)
    with patch('app.services.pagarme.criar_pedido_pix', return_value={
            'ok': True, 'order_id': 'or_1', 'charge_id': 'ch_1',
            'qr_code': 'qr', 'qr_code_url': 'u', 'expira_em': fim_qr}):
        pag, erros = loja_pagamento.iniciar_pix(compra.pedido_principal)
    assert pag and not erros
    db.session.refresh(compra)
    assert compra.expira_em == fim_qr + timedelta(minutes=5)


# ── 50. Sob encomenda: produto abre na primeira data com saldo ──────────

def test_sob_encomenda_abre_na_primeira_data_com_saldo(app, monkeypatch):
    from app.extensions import db
    from app.services import loja_plano_dia
    from app.services.loja_catalogo import _slugify
    from app.utils import hoje
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    p = _produto(db, nome='Caixa de Mini', sob_encomenda=True)
    d2, d3 = hoje() + timedelta(days=2), hoje() + timedelta(days=3)
    loja_plano_dia.definir('produto', p.id, d2, 0)
    html = app.test_client().get(f'/loja/{_slugify(p.nome)}-p{p.id}').get_data(as_text=True)
    assert f'value="{d3.isoformat()}"' in html
    assert f'min="{d2.isoformat()}"' in html
    assert 'Primeira data disponível' in html


def test_sob_encomenda_com_d2_livre_segue_no_d2(app, monkeypatch):
    from app.extensions import db
    from app.services.loja_catalogo import _slugify
    from app.utils import hoje
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    p = _produto(db, nome='Caixa de Mini', sob_encomenda=True)
    d2 = hoje() + timedelta(days=2)
    html = app.test_client().get(f'/loja/{_slugify(p.nome)}-p{p.id}').get_data(as_text=True)
    assert f'value="{d2.isoformat()}"' in html
    assert 'Primeira data disponível' not in html

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


# ── 49. NF: entrega com CNPJ também passa pela trava de endereço ────────

def test_nf_cnpj_com_entrega_incompleta_nao_vai_a_sefaz(app):
    from app.extensions import db
    from app.services import tiny_nf
    from app.utils import agora
    ped = _pedido(db, _produto(db), status='pago', pago_em=agora(),
                  modo='agendada', endereco_entrega='Rua A, 10',
                  endereco_logradouro='Rua A', endereco_numero='10',
                  endereco_cidade='São Paulo', endereco_cep='04571-010')
    cliente = {'nome': 'Empresa X', 'tipo_pessoa': 'J',
               'cpf_cnpj': '11222333000181'}
    with patch('app.services.fiscal_online.payload_cliente',
               return_value=(cliente, None)), \
            patch('app.services.tiny.incluir_nota_fiscal') as incluir:
        res = tiny_nf._emitir_nf_pedido(ped)
    incluir.assert_not_called()
    assert res['ok'] is False
    assert 'Endereço de entrega incompleto (bairro, UF)' in res['msg']


# ── 41. Reserva física congelada por pedido ─────────────────────────────

def _cesta_reservada(db):
    """Cesta Café = 2 Croissants, reservada por um pedido; Pain au Chocolat
    com 3 reservados por OUTRO pedido."""
    from app.models import (
        AppConfig,
        EstoqueLoja,
        Loja,
        Produto,
        ProdutoItem,
        Receita,
    )
    from app.services import loja_estoque_reserva
    from app.utils import agora
    loja = Loja(nome='Brooklin', endereco='Rua Y, 1', ativa=True)
    a = Receita(nome='Croissant', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=90)
    b = Receita(nome='Pain au Chocolat', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=90)
    cesta = Produto(nome='Cesta Cafe', categoria='Cestas', preco_site=80.0,
                    ativo=True, imagem_dropbox_url='https://x/c.jpg')
    db.session.add_all([loja, a, b, cesta])
    db.session.flush()
    db.session.add(ProdutoItem(produto_id=cesta.id, tipo='receita',
                               receita_id=a.id, item_nome=a.nome, quantidade=2))
    ela = EstoqueLoja(loja_id=loja.id, receita_id=a.id, quantidade=50)
    elb = EstoqueLoja(loja_id=loja.id, receita_id=b.id, quantidade=50,
                      quantidade_reservada=3)
    db.session.add_all([ela, elb])
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', loja.id)
    ped = _pedido(db, cesta, loja_retirada_id=loja.id)
    loja_estoque_reserva.reservar(ped, loja_id=loja.id)
    db.session.commit()
    assert ela.quantidade_reservada == 2
    # O dono troca o croissant por pain au chocolat (a tela recria os itens).
    ProdutoItem.query.filter_by(produto_id=cesta.id).delete()
    db.session.add(ProdutoItem(produto_id=cesta.id, tipo='receita',
                               receita_id=b.id, item_nome=b.nome, quantidade=2))
    ped.reserva_expira_em = agora() - timedelta(minutes=1)
    db.session.commit()
    db.session.expire_all()
    return loja, ped, ela.id, elb.id


def test_expiracao_devolve_a_reserva_original_mesmo_com_cesta_editada(app):
    from app.extensions import db
    from app.models import EstoqueLoja, ReservaEstoqueSite
    from app.services import loja_estoque_reserva
    from app.utils import agora
    _loja, ped, ida, idb = _cesta_reservada(db)
    # Sem tentativa de pagamento: nada vivo no gateway, o cron cancela.
    loja_estoque_reserva.liberar_expirados(agora_=agora())
    db.session.expire_all()
    assert db.session.get(EstoqueLoja, ida).quantidade_reservada == 0
    assert db.session.get(EstoqueLoja, idb).quantidade_reservada == 3
    assert ReservaEstoqueSite.query.filter_by(pedido_id=ped.id).count() == 0


def test_pagamento_consome_a_reserva_original_mesmo_com_cesta_editada(app):
    from app.extensions import db
    from app.models import EstoqueLoja, ReservaEstoqueSite
    from app.services import loja_estoque_reserva
    from app.utils import agora
    loja, ped, ida, idb = _cesta_reservada(db)
    ped.reserva_expira_em = agora() + timedelta(minutes=10)   # ainda viva
    db.session.commit()
    loja_estoque_reserva.consumir(ped, loja_id=loja.id)
    db.session.commit()
    db.session.expire_all()
    assert db.session.get(EstoqueLoja, ida).quantidade_reservada == 0
    assert db.session.get(EstoqueLoja, idb).quantidade_reservada == 3
    assert ReservaEstoqueSite.query.filter_by(pedido_id=ped.id).count() == 0


def test_pedido_anterior_ao_snapshot_libera_pela_reexpansao(app):
    """Pedido reservado antes do deploy (sem `ReservaEstoqueSite`): a
    liberação segue o caminho antigo, pelo cadastro."""
    from app.extensions import db
    from app.models import EstoqueLoja, Loja
    from app.services import loja_estoque_reserva
    from app.utils import agora
    loja = Loja(nome='Brooklin', endereco='Rua Y, 1', ativa=True)
    db.session.add(loja)
    db.session.commit()
    prod = _produto(db)
    el = EstoqueLoja(loja_id=loja.id, produto_id=prod.id, quantidade=10,
                     quantidade_reservada=5)
    db.session.add(el)
    ped = _pedido(db, prod, qtd=2, loja_retirada_id=loja.id)
    ped.reserva_expira_em = agora() + timedelta(minutes=5)
    db.session.commit()
    loja_estoque_reserva.liberar(ped, loja_id=loja.id)
    db.session.commit()
    assert el.quantidade_reservada == 3


# ── 34. Entradas malformadas não dão 500 ────────────────────────────────

def test_payloads_malformados_nao_dao_500(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['PAGARME_WEBHOOK_SECRET'] = 'segredo'
    c = app.test_client()
    casos = [
        c.post('/loja/checkout', data={'itens_json': '{"a":1}', 'nome': 'X'}),
        c.post('/loja/checkout', data={'itens_json': '[1, null, "x"]'}),
        c.post('/loja/checkout', data={'itens_json': '[' * 5000 + ']' * 5000}),
        c.post('/loja/api/carrinho', json=[{'kind': 'produto', 'id': 1}]),
        c.post('/loja/api/carrinho', json={'itens': 'abc'}),
        c.post('/loja/api/carrinho', json={'itens': [None, 3, {'kind': 'produto', 'id': 10**30}]}),
        c.get('/loja/carrinho?add=p1:%C2%B2'),
        c.post('/loja/webhook/pagarme?k=%C3%A7', json={'type': 'order.paid'}),
    ]
    assert [r.status_code for r in casos if r.status_code >= 500] == []
    assert casos[-1].status_code == 401


# ── 33. Preço com mais de 2 casas ───────────────────────────────────────

def test_preco_legado_com_3_casas_fecha_em_centavos(app):
    from datetime import datetime

    from app.extensions import db
    from app.models import AppConfig, Loja, PedidoOnline
    from app.services import loja_checkout, pagarme
    from app.services.compra_kits import valor_cobranca
    a = _produto(db, nome='A', preco=10.005)
    b = _produto(db, nome='B', preco=10.005)
    loja = Loja(nome='Brooklin', endereco='R', ativa=True)
    db.session.add(loja)
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', loja.id)
    base = datetime.now().replace(hour=9, minute=0)
    form = {'nome': 'Maria', 'sobrenome': 'Silva', 'email': 'm@example.com',
            'cpf': '52998224725', 'aceite_lgpd': '1',
            'modo_entrega': 'retirada', 'loja_id': str(loja.id),
            'data_entrega': (base + timedelta(days=2)).date().isoformat(),
            'janela_entrega': '10:00–11:00', 'cep': '04077-000',
            'logradouro': 'Rua Y', 'numero': '20', 'bairro': 'Moema',
            'cidade': 'São Paulo', 'uf': 'SP'}
    ped, erros = loja_checkout.criar_pedido(form, [
        {'kind': 'produto', 'id': a.id, 'qtd': 3},
        {'kind': 'produto', 'id': b.id, 'qtd': 1}], base=base)
    assert not erros, erros
    db.session.expire_all()
    ped = db.session.get(PedidoOnline, ped.id)
    soma_linhas = sum(i.subtotal for i in ped.itens)
    soma_unit = sum(i.preco_unitario * i.quantidade for i in ped.itens)
    assert soma_linhas == soma_unit == ped.subtotal == Decimal('40.04')
    soma_pg = sum(i['amount'] * i['quantity'] for i in pagarme._payload_items(ped))
    assert soma_pg == pagarme._centavos(valor_cobranca(ped))


def test_telas_recusam_preco_com_3_casas(app, owner_user):
    from app.extensions import db
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['LOJA_HOSTS'] = 'opao.online'   # telas do admin fora da loja
    p = _produto(db, preco=20.0)
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(owner_user.id)
        s['_fresh'] = True
    r = c.post('/receitas/precos/salvar-campo',
               json={'tipo': 'produto', 'id': p.id, 'campo': 'preco_site',
                     'valor': '10,005'})
    assert r.status_code == 400 and 'casas decimais' in r.get_json()['erro']
    r = c.post(f'/admin/loja-online/catalogo/preco/produto/{p.id}',
               json={'preco': '10.005'})
    assert r.status_code == 400
    r = c.post('/receitas/precos', data={
        f'preco_loja_p{p.id}': '', f'preco_site_p{p.id}': '10,005',
        f'preco_atacado_p{p.id}': '', f'preco_interno_p{p.id}': ''},
        follow_redirects=True)
    assert 'casas decimais' in r.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(type(p), p.id).preco_site == 20.0


# ── 37/42. CEP validado; esgotado conferido nas datas do calendário ─────

_FORM_RETIRADA = {'nome': 'Maria', 'sobrenome': 'Silva', 'email': 'm@x.com',
                  'cpf': '52998224725', 'aceite_lgpd': '1',
                  'modo_entrega': 'retirada', 'janela_entrega': '08:00–09:00',
                  'cep': '04077-000', 'logradouro': 'Rua X', 'numero': '10',
                  'bairro': 'Moema', 'cidade': 'São Paulo', 'uf': 'SP'}


def _loja_site(db):
    from app.models import AppConfig, Loja
    loja = Loja(nome='Brooklin', endereco='Rua Y, 1', ativa=True)
    db.session.add(loja)
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', loja.id)
    return loja


def test_item_planejado_so_no_ultimo_dia_da_agenda_e_aceito(app):
    from app.extensions import db
    from app.services import loja_checkout, loja_plano_dia
    from app.utils import agora, hoje
    loja = _loja_site(db)
    p = _produto(db, nome='Panettone', preco=90.0)
    datas = loja_checkout.datas_disponiveis('retirada', base=agora())
    ultimo = datas[-1]
    for i in range(0, 14):
        loja_plano_dia.definir('produto', p.id, hoje() + timedelta(days=i), 0)
    loja_plano_dia.definir('produto', p.id, ultimo, 20)
    ped, erros = loja_checkout.criar_pedido(
        {**_FORM_RETIRADA, 'loja_id': str(loja.id),
         'data_entrega': ultimo.isoformat()},
        [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=agora())
    assert erros == [] and ped is not None


def test_cep_longo_e_recusado_com_mensagem(app):
    from app.extensions import db
    from app.models import PedidoOnline
    from app.services import loja_checkout
    from app.utils import agora
    loja = _loja_site(db)
    p = _produto(db)
    dia = loja_checkout.datas_disponiveis('retirada', base=agora())[2]
    ped, erros = loja_checkout.criar_pedido(
        {**_FORM_RETIRADA, 'loja_id': str(loja.id), 'cep': '04077-000 / 04078-100',
         'data_entrega': dia.isoformat()},
        [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=agora())
    assert ped is None
    assert 'Informe um CEP válido (8 números).' in erros
    assert PedidoOnline.query.count() == 0


def test_cep_sem_hifen_e_gravado_no_formato_da_mascara(app):
    from app.extensions import db
    from app.services import loja_checkout
    from app.utils import agora
    loja = _loja_site(db)
    p = _produto(db)
    dia = loja_checkout.datas_disponiveis('retirada', base=agora())[2]
    ped, erros = loja_checkout.criar_pedido(
        {**_FORM_RETIRADA, 'loja_id': str(loja.id), 'cep': '04077000',
         'data_entrega': dia.isoformat()},
        [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=agora())
    assert erros == []
    assert ped.endereco_cep == '04077-000'


def test_edicao_admin_recusa_campo_maior_que_a_coluna(app, owner_user):
    from app.extensions import db
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['LOJA_HOSTS'] = 'opao.online'
    ped = _pedido(db, _produto(db), modo='agendada', endereco_entrega='Rua A, 10')
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(owner_user.id)
        s['_fresh'] = True
    r = c.post(f'/admin/loja-online/pedidos/{ped.codigo}/editar', data={
        'nome_cliente': 'Maria', 'email_cliente': 'm@x.com',
        'modo_entrega': 'agendada', 'endereco_bairro': 'B' * 150,
        'endereco_cep': '0407'}, follow_redirects=True)
    h = r.get_data(as_text=True)
    assert 'Bairro: no máximo 100 caracteres.' in h
    assert 'CEP inválido (8 números).' in h
    db.session.expire_all()
    assert db.session.get(type(ped), ped.id).endereco_bairro is None

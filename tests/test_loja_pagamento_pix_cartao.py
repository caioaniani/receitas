"""Núcleo da Fase 4 — Pagar.me: Pix, cartão, webhook, baixa de estoque.

Foco: integridade de dinheiro. A baixa de estoque acontece SÓ no webhook
'paid' (nunca no retorno do checkout). Idempotência via PagarmeEvento.
Estorno reverte a baixa. Valor em centavos no payload.

Pagar.me não é chamado de verdade — mockamos requests.post/delete pra
testar o SHAPE do payload e o caminho de erro.
"""
from decimal import Decimal
from unittest.mock import patch


def _admin(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Adm', login='adm', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def _loja_site(db, nome='Loja Anesio Pinto Rosa'):
    from app.models import Loja
    loja = Loja(nome=nome, ativa=True, endereco='Anésio Pinto Rosa, 78')
    db.session.add(loja)
    db.session.commit()
    return loja


def _produto(db, nome='Box Mimo', preco=20.0):
    from app.models import Produto
    p = Produto(nome=nome, categoria='Cestas', preco_site=preco,
                imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


def _pedido_com_item(db, produto, qtd=2, modo='retirada',
                     loja_retirada_id=None, frete=Decimal('0')):
    from app.models import PedidoOnline, PedidoOnlineItem
    p = PedidoOnline(
        nome_cliente='Maria', email_cliente='m@x.com',
        telefone_cliente='11999998888',
        modo_entrega=modo, loja_retirada_id=loja_retirada_id,
        frete_valor=frete,
        subtotal=Decimal('0'), valor_total=Decimal('0'))
    db.session.add(p)
    db.session.flush()
    p.itens.append(PedidoOnlineItem(
        kind='produto', produto_id=produto.id, nome=produto.nome,
        preco_unitario=Decimal(str(produto.preco_site)), quantidade=qtd,
        subtotal=Decimal(str(produto.preco_site)) * qtd))
    p.recalcular_total()
    db.session.commit()
    return p


def _fake_resp(status, body):
    class R:
        status_code = status
        text = ''
        def json(self): return body
    return R()


# ── pagarme.py: centavos + payloads ──────────────────────────────────

def test_pagarme_centavos_arredonda_correto(app):
    from decimal import Decimal as D

    from app.services import pagarme
    with app.app_context():
        assert pagarme._centavos(D('33.33')) == 3333
        assert pagarme._centavos(D('33.335')) == 3334  # HALF_UP
        assert pagarme._centavos(0) == 0
        assert pagarme._centavos(None) == 0


def test_criar_pedido_pix_envia_centavos_e_codigo(app):
    from app.extensions import db
    from app.services import pagarme
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_abc'
        p = _produto(db, preco=20.0)
        ped = _pedido_com_item(db, p, qtd=2)  # total = 40,00
        body = {'id': 'or_1', 'charges': [{'id': 'ch_1', 'status': 'pending',
                'last_transaction': {'qr_code': 'EMV-XYZ', 'qr_code_url': 'https://qr/1'}}]}
        with patch('app.services.pagarme.requests.post',
                   return_value=_fake_resp(200, body)) as post:
            res = pagarme.criar_pedido_pix(ped, expira_em_min=15)
        assert res['ok'] is True
        assert res['order_id'] == 'or_1' and res['charge_id'] == 'ch_1'
        assert res['qr_code'] == 'EMV-XYZ'
        # Payload bate o contrato v5
        payload = post.call_args[1]['json']
        assert payload['code'] == ped.codigo
        assert payload['payments'][0]['payment_method'] == 'pix'
        assert payload['payments'][0]['amount'] == 4000  # 40,00 em centavos
        assert payload['payments'][0]['pix']['expires_in'] == 15 * 60
        # Items: amount é POR UNIDADE; total = sum(amount * quantity)
        soma_items = sum(i['amount'] * i['quantity']
                         for i in payload['items'])
        assert soma_items == 4000  # nada de frete neste pedido


def test_criar_pedido_cartao_token_e_parcelas(app):
    from app.extensions import db
    from app.services import pagarme
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_abc'
        p = _produto(db, preco=50.0)
        ped = _pedido_com_item(db, p, qtd=1, frete=Decimal('15'))  # 65,00
        body = {'id': 'or_2', 'charges': [{'id': 'ch_2', 'status': 'paid'}]}
        with patch('app.services.pagarme.requests.post',
                   return_value=_fake_resp(200, body)) as post:
            res = pagarme.criar_pedido_cartao(ped, 'tok_abc', parcelas=3)
        assert res['ok'] is True and res['status'] == 'paid'
        payload = post.call_args[1]['json']
        cc = payload['payments'][0]
        assert cc['payment_method'] == 'credit_card'
        assert cc['amount'] == 6500
        assert cc['credit_card']['card_token'] == 'tok_abc'
        assert cc['credit_card']['installments'] == 3
        # billing_address (antifraude) vai em credit_card.card.billing_address
        billing = cc['credit_card']['card']['billing_address']
        assert billing['country'] == 'BR' and billing['state'] == 'SP'
        # Frete entra como item separado pra somar
        descs = [i['description'] for i in payload['items']]
        assert 'Frete' in descs


def test_cartao_billing_address_do_form(app):
    """O endereço de cobrança informado no form vai pro payload normalizado."""
    from app.extensions import db
    from app.services import pagarme
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_abc'
        p = _produto(db, preco=10.0)
        ped = _pedido_com_item(db, p, qtd=1)
        body = {'id': 'or_b', 'charges': [{'id': 'ch_b', 'status': 'paid'}]}
        billing = {'line_1': 'Rua X, 10', 'zip_code': '04077-000',
                   'city': 'São Paulo', 'state': 'sp', 'country': 'br'}
        with patch('app.services.pagarme.requests.post',
                   return_value=_fake_resp(200, body)) as post:
            pagarme.criar_pedido_cartao(ped, 'tok', parcelas=1, billing=billing)
        ba = post.call_args[1]['json']['payments'][0]['credit_card']['card']['billing_address']
        assert ba['line_1'] == 'Rua X, 10'
        assert ba['zip_code'] == '04077000'   # só dígitos
        assert ba['state'] == 'SP'            # uppercase, 2 chars
        assert ba['country'] == 'BR'


def test_criar_pedido_cartao_pagarme_recusa(app):
    """Quando o Pagar.me devolve 422 (ex: token expirado/inválido),
    criar_pedido_cartao propaga ok=False com o motivo."""
    from app.extensions import db
    from app.services import pagarme
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_abc'
        p = _produto(db)
        ped = _pedido_com_item(db, p)
        with patch('app.services.pagarme.requests.post',
                   return_value=_fake_resp(422,
                                           {'message': 'card token invalid'})):
            res = pagarme.criar_pedido_cartao(ped, 'tok_x', parcelas=1)
        assert res['ok'] is False
        assert 'invalid' in res['erro'].lower() or res.get('http') == 422


def test_cancelar_charge(app):
    from app.services import pagarme
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_abc'
        with patch('app.services.pagarme.requests.delete',
                   return_value=_fake_resp(200, {})) as d:
            r = pagarme.cancelar_charge('ch_1')
        assert r['ok'] is True
        # URL bate
        assert 'ch_1' in d.call_args[0][0]


# ── loja_pagamento.py: orquestração ───────────────────────────────────

def test_iniciar_pix_cria_pagamento_e_grava_qr(app):
    from app.extensions import db
    from app.models import PagamentoOnline
    from app.services import loja_pagamento
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_abc'
        _loja_site(db)
        p = _produto(db, preco=20.0)
        ped = _pedido_com_item(db, p)
        body = {'id': 'or_1', 'charges': [{'id': 'ch_1', 'status': 'waiting_payment',
                'last_transaction': {'qr_code': 'EMV-1', 'qr_code_url': 'https://qr/1'}}]}
        with patch('app.services.pagarme.requests.post',
                   return_value=_fake_resp(200, body)):
            pag, erros = loja_pagamento.iniciar_pix(ped)
        assert erros == []
        assert pag.metodo == 'pix' and pag.status == 'pendente'
        assert pag.pix_qr_code == 'EMV-1'
        assert pag.pagarme_order_id == 'or_1'
        # Persistiu de fato
        assert PagamentoOnline.query.filter_by(pedido_id=ped.id).count() == 1


def test_iniciar_pix_erro_marca_falhou(app):
    from app.extensions import db
    from app.services import loja_pagamento
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_abc'
        _loja_site(db)
        p = _produto(db)
        ped = _pedido_com_item(db, p)
        with patch('app.services.pagarme.requests.post',
                   return_value=_fake_resp(401, {'message': 'unauthorized'})):
            pag, erros = loja_pagamento.iniciar_pix(ped)
        assert pag is None
        assert erros
        from app.models import PagamentoOnline
        salvo = PagamentoOnline.query.filter_by(pedido_id=ped.id).first()
        assert salvo.status == 'falhou'


def test_iniciar_cartao_marca_substituicao_de_pix_pendente(app):
    from app.extensions import db
    from app.models import PagamentoOnline
    from app.services import loja_pagamento
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_abc'
        _loja_site(db)
        p = _produto(db)
        ped = _pedido_com_item(db, p)
        # Já tem um Pix pendente
        body_pix = {'id': 'or_p', 'charges': [{'id': 'ch_p', 'status': 'waiting_payment',
                    'last_transaction': {'qr_code': 'E'}}]}
        with patch('app.services.pagarme.requests.post',
                   return_value=_fake_resp(200, body_pix)):
            loja_pagamento.iniciar_pix(ped)
        body_c = {'id': 'or_c', 'charges': [{'id': 'ch_c', 'status': 'paid'}]}
        with patch('app.services.pagarme.requests.post',
                   return_value=_fake_resp(200, body_c)):
            pag, erros = loja_pagamento.iniciar_cartao(ped, 'tok', parcelas=1)
        assert erros == []
        pix = PagamentoOnline.query.filter_by(pedido_id=ped.id, metodo='pix').first()
        assert pix.status == 'falhou'  # substituído
        assert pag.metodo == 'cartao'


# ── Webhook: idempotência + baixa de estoque + estorno ───────────────

def _setup_loja_estoque(db, ped, produto, qtd_atual=100):
    """Garante linha de EstoqueLoja com saldo pra o produto."""
    from app.models import EstoqueLoja
    el = EstoqueLoja(loja_id=ped.loja_retirada_id, produto_id=produto.id,
                     quantidade=qtd_atual)
    db.session.add(el)
    db.session.commit()
    return el


# ── Conciliação manual (rede de segurança pra webhook perdido) ────────

def test_conciliar_marca_pago_quando_gateway_confirma(app):
    """Webhook não chegou, mas o Pagar.me confirma pago → ?aplicar marca o
    pedido pago + baixa estoque. Dry-run não toca em nada."""
    from app.extensions import db
    from app.models import MovEstoqueLoja, PagamentoOnline
    from app.services import loja_pagamento
    with app.app_context():
        loja = _loja_site(db)
        p = _produto(db)
        ped = _pedido_com_item(db, p, qtd=2, modo='retirada',
                                loja_retirada_id=loja.id)
        el = _setup_loja_estoque(db, ped, p, qtd_atual=10)
        db.session.add(PagamentoOnline(
            pedido_id=ped.id, metodo='pix', status='pendente',
            valor=ped.valor_total, pagarme_order_id='or_concilia1'))
        db.session.commit()
        confirma = {'ok': True, 'pago': True, 'status': 'paid'}
        # dry-run: reporta pago mas NÃO aplica
        with patch('app.services.pagarme.consultar_order',
                   return_value=confirma):
            r_dry = loja_pagamento.conciliar_pedido(ped.codigo, aplicar=False)
        assert r_dry['pagarme_pago'] is True
        db.session.refresh(ped)
        assert ped.status == 'aguardando_pagamento'
        # aplicar=True: marca pago + baixa estoque
        with patch('app.services.pagarme.consultar_order',
                   return_value=confirma), \
             patch('app.services.email.enviar_confirmacao_pedido',
                   return_value={'ok': True}):
            r = loja_pagamento.conciliar_pedido(ped.codigo, aplicar=True)
        assert r['acao'] == 'MARCADO PAGO'
        db.session.refresh(ped)
        db.session.refresh(el)
        assert ped.status == 'pago'
        assert el.quantidade == 8  # baixou 2
        assert MovEstoqueLoja.query.filter_by(tipo='venda_site').count() >= 1


def test_conciliar_nao_marca_se_gateway_nao_confirma(app):
    """Pagar.me NÃO confirma pago → conciliar não toca no pedido (não
    inventa pagamento)."""
    from app.extensions import db
    from app.models import PagamentoOnline
    from app.services import loja_pagamento
    with app.app_context():
        loja = _loja_site(db)
        p = _produto(db)
        ped = _pedido_com_item(db, p, qtd=1, modo='retirada',
                                loja_retirada_id=loja.id)
        db.session.add(PagamentoOnline(
            pedido_id=ped.id, metodo='pix', status='pendente',
            valor=ped.valor_total, pagarme_order_id='or_naopago'))
        db.session.commit()
        with patch('app.services.pagarme.consultar_order',
                   return_value={'ok': True, 'pago': False,
                                 'status': 'pending'}):
            r = loja_pagamento.conciliar_pedido(ped.codigo, aplicar=True)
        assert r['pagarme_pago'] is False
        assert 'NÃO confirma' in r['acao']
        db.session.refresh(ped)
        assert ped.status == 'aguardando_pagamento'


def test_conciliar_idempotente_nao_baixa_duas_vezes(app):
    """Rodar conciliar 2x (ou webhook chegar depois) não duplica baixa —
    _marcar_pago é no-op se já pago."""
    from app.extensions import db
    from app.models import MovEstoqueLoja, PagamentoOnline
    from app.services import loja_pagamento
    with app.app_context():
        loja = _loja_site(db)
        p = _produto(db)
        ped = _pedido_com_item(db, p, qtd=2, modo='retirada',
                                loja_retirada_id=loja.id)
        el = _setup_loja_estoque(db, ped, p, qtd_atual=10)
        db.session.add(PagamentoOnline(
            pedido_id=ped.id, metodo='pix', status='pendente',
            valor=ped.valor_total, pagarme_order_id='or_idem'))
        db.session.commit()
        confirma = {'ok': True, 'pago': True, 'status': 'paid'}
        with patch('app.services.pagarme.consultar_order',
                   return_value=confirma), \
             patch('app.services.email.enviar_confirmacao_pedido',
                   return_value={'ok': True}):
            loja_pagamento.conciliar_pedido(ped.codigo, aplicar=True)
            r2 = loja_pagamento.conciliar_pedido(ped.codigo, aplicar=True)
        assert 'já estava pago' in r2['acao']
        db.session.refresh(el)
        assert el.quantidade == 8  # baixou 2 UMA vez só
        assert MovEstoqueLoja.query.filter_by(tipo='venda_site').count() == 1


def test_webhook_paid_marca_pago_e_baixa_estoque(app):
    from app.extensions import db
    from app.models import MovEstoqueLoja
    from app.services import loja_pagamento
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_abc'
        loja = _loja_site(db)
        p = _produto(db, preco=20.0)
        ped = _pedido_com_item(db, p, qtd=3, modo='retirada',
                                loja_retirada_id=loja.id)
        _setup_loja_estoque(db, ped, p, qtd_atual=10)
        # Cria um pagamento ligado a um order_id pra o webhook achar
        body = {'id': 'or_x', 'charges': [{'id': 'ch_x', 'status': 'waiting',
                'last_transaction': {'qr_code': 'E'}}]}
        with patch('app.services.pagarme.requests.post',
                   return_value=_fake_resp(200, body)):
            loja_pagamento.iniciar_pix(ped)
        # Evento do webhook
        evento = {'id': 'evt_1', 'type': 'order.paid',
                  'data': {'id': 'or_x', 'code': ped.codigo}}
        res = loja_pagamento.processar_webhook(evento)
        assert res['ok'] is True and res.get('pago') is True
        db.session.refresh(ped)
        assert ped.status == 'pago' and ped.pago_em is not None
        # MovEstoqueLoja('venda_site', qtd=3, ref="Site #<codigo>")
        movs = MovEstoqueLoja.query.filter_by(
            tipo='venda_site',
            referencia=f'Site #{ped.codigo}').all()
        assert len(movs) == 1 and movs[0].quantidade == 3


def test_webhook_paid_idempotente(app):
    """Reentrega do MESMO evento_id NÃO duplica baixa."""
    from app.extensions import db
    from app.models import MovEstoqueLoja
    from app.services import loja_pagamento
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_abc'
        loja = _loja_site(db)
        p = _produto(db)
        ped = _pedido_com_item(db, p, qtd=2, modo='retirada',
                                loja_retirada_id=loja.id)
        _setup_loja_estoque(db, ped, p, qtd_atual=10)
        body = {'id': 'or_y', 'charges': [{'id': 'ch_y', 'status': 'pending',
                'last_transaction': {'qr_code': 'E'}}]}
        with patch('app.services.pagarme.requests.post',
                   return_value=_fake_resp(200, body)):
            loja_pagamento.iniciar_pix(ped)
        evento = {'id': 'evt_dup', 'type': 'order.paid',
                  'data': {'id': 'or_y', 'code': ped.codigo}}
        r1 = loja_pagamento.processar_webhook(evento)
        r2 = loja_pagamento.processar_webhook(evento)
        assert r1.get('pago') and r2.get('duplicado') is True
        movs = MovEstoqueLoja.query.filter_by(
            tipo='venda_site',
            referencia=f'Site #{ped.codigo}').all()
        assert len(movs) == 1  # NÃO duplicou


def test_webhook_refunded_NAO_estorna_automaticamente(app):
    """Estorno automático DESATIVADO (decisão do dono 18/06/2026): um
    cancelamento em massa no gateway (bug/abuso, como já ocorreu no VNDA)
    NÃO pode cancelar pedido nem devolver estoque por aqui. O webhook só
    registra o evento; o estorno é sempre manual (`reembolsar_pedido`)."""
    from app.extensions import db
    from app.models import MovEstoqueLoja
    from app.services import loja_pagamento
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_abc'
        loja = _loja_site(db)
        p = _produto(db)
        ped = _pedido_com_item(db, p, qtd=2, modo='retirada',
                                loja_retirada_id=loja.id)
        el = _setup_loja_estoque(db, ped, p, qtd_atual=10)
        body = {'id': 'or_r', 'charges': [{'id': 'ch_r', 'status': 'pending',
                'last_transaction': {'qr_code': 'E'}}]}
        with patch('app.services.pagarme.requests.post',
                   return_value=_fake_resp(200, body)):
            loja_pagamento.iniciar_pix(ped)
        # Marca pago primeiro
        loja_pagamento.processar_webhook(
            {'id': 'evt_pago', 'type': 'order.paid',
             'data': {'id': 'or_r', 'code': ped.codigo}})
        db.session.refresh(el)
        assert el.quantidade == 8  # baixou 2
        # Webhook de estorno chega: NÃO deve mexer no pedido nem no estoque.
        res = loja_pagamento.processar_webhook(
            {'id': 'evt_ref', 'type': 'charge.refunded',
             'data': {'id': 'or_r', 'code': ped.codigo}})
        assert res.get('estorno_ignorado') == 'charge.refunded'
        db.session.refresh(ped)
        db.session.refresh(el)
        assert ped.status != 'cancelado'   # pedido intacto
        assert el.quantidade == 8          # estoque NÃO foi devolvido
        assert MovEstoqueLoja.query.filter_by(
            tipo='venda_site_estorno').count() == 0


# ── Rotas + webhook seguro ────────────────────────────────────────────

def test_qr_data_uri_gera_png(app):
    from app.services import pagarme
    with app.app_context():
        uri = pagarme.qr_data_uri('00020101BR.GOV.BCB.PIX-EMV-TESTE6304ABCD')
        assert uri and uri.startswith('data:image/png;base64,')
        assert pagarme.qr_data_uri('') is None


def test_pagamento_mostra_qr_gerado_do_emv(app, monkeypatch):
    """Quando o Pix tem EMV, a tela mostra um <img> com QR data-URI
    (gerado no servidor) — não depende do qr_code_url do Pagar.me."""
    from decimal import Decimal as D

    from app.extensions import db
    from app.models import PagamentoOnline
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin(app)
    _loja_site(db)
    p = _produto(db)
    ped = _pedido_com_item(db, p)
    pag = PagamentoOnline(pedido_id=ped.id, metodo='pix', valor=D('20'),
                          status='pendente',
                          pix_qr_code='00020101BR.GOV.BCB.PIX6304XYZ',
                          pix_qr_code_url='')
    db.session.add(pag)
    db.session.commit()
    r = c.get(f'/loja/pedido/{ped.codigo}/pagamento')
    assert r.status_code == 200
    assert b'data:image/png;base64,' in r.data       # QR embutido
    assert b'00020101BR.GOV.BCB.PIX6304XYZ' in r.data  # copia-e-cola = EMV


def test_csp_loja_libera_pagarme(app):
    """A página da loja precisa liberar o SDK do Pagar.me (script + connect)
    pra tokenização do cartão funcionar."""
    c = app.test_client()
    r = c.get('/loja/robots.txt')  # rota da loja sempre acessível
    csp = r.headers.get('Content-Security-Policy', '')
    assert 'checkout.pagar.me' in csp
    assert 'connect-src' in csp and 'api.pagar.me' in csp


def test_webhook_paid_dispara_email_confirmacao(app):
    """Quando o pedido vira pago, manda e-mail de confirmação (best-effort)."""
    from unittest.mock import patch as _patch

    from app.extensions import db
    from app.services import loja_pagamento
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_abc'
        app.config['POSTMARK_SERVER_TOKEN'] = 'tok'
        loja = _loja_site(db)
        p = _produto(db)
        ped = _pedido_com_item(db, p, modo='retirada', loja_retirada_id=loja.id)
        _setup_loja_estoque(db, ped, p)
        body = {'id': 'or_e', 'charges': [{'id': 'ch_e', 'status': 'pending',
                'last_transaction': {'qr_code': 'E'}}]}
        with _patch('app.services.pagarme.requests.post',
                    return_value=_fake_resp(200, body)):
            loja_pagamento.iniciar_pix(ped)
        with _patch('app.services.email.enviar_confirmacao_pedido',
                    return_value={'ok': True}) as envia:
            loja_pagamento.processar_webhook(
                {'id': 'evt_mail', 'type': 'order.paid',
                 'data': {'id': 'or_e', 'code': ped.codigo}})
        envia.assert_called_once()


# ── NF automática no pagamento (decisão do dono 19/06/2026) ───────────

def test_paid_emite_nf_e_envia_email_da_nf(app):
    """Webhook 'paid' → emite NF no Tiny + manda e-mail dedicado com link
    da DANFE. Best-effort: ambos rodam dentro de _marcar_pago, depois do
    estoque e do e-mail de confirmação."""
    from unittest.mock import patch as _patch

    from app.extensions import db
    from app.services import loja_pagamento
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_abc'
        app.config['POSTMARK_SERVER_TOKEN'] = 'tok'  # disponivel() → True
        loja = _loja_site(db)
        p = _produto(db)
        ped = _pedido_com_item(db, p, qtd=1, modo='retirada',
                                loja_retirada_id=loja.id)
        _setup_loja_estoque(db, ped, p)
        body = {'id': 'or_nf', 'charges': [{'id': 'ch_nf', 'status': 'pending',
                'last_transaction': {'qr_code': 'E'}}]}
        with _patch('app.services.pagarme.requests.post',
                    return_value=_fake_resp(200, body)):
            loja_pagamento.iniciar_pix(ped)
        with _patch('app.services.tiny_nf.emitir_nf',
                    return_value={'ok': True, 'nota_fiscal_id': 'nf-77'}) as emi, \
             _patch('app.services.email.enviar_nf_emitida',
                    return_value={'ok': True}) as mail:
            loja_pagamento.processar_webhook(
                {'id': 'evt_nf', 'type': 'order.paid',
                 'data': {'id': 'or_nf', 'code': ped.codigo}})
        emi.assert_called_once()
        mail.assert_called_once()


def test_paid_continua_pago_mesmo_se_nf_falhar(app):
    """NF é BEST-EFFORT: se o Tiny rejeitar ou estiver fora, o pedido SEGUE
    pago/estoque baixado, e o e-mail da NF NÃO sai (não vamos mandar e-mail
    de NF que não existe). A NF fica pra reemitir manual depois."""
    from unittest.mock import patch as _patch

    from app.extensions import db
    from app.models import PedidoOnline
    from app.services import loja_pagamento
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_abc'
        loja = _loja_site(db)
        p = _produto(db)
        ped = _pedido_com_item(db, p, qtd=1, modo='retirada',
                                loja_retirada_id=loja.id)
        _setup_loja_estoque(db, ped, p, qtd_atual=10)
        body = {'id': 'or_x', 'charges': [{'id': 'ch_x', 'status': 'pending',
                'last_transaction': {'qr_code': 'E'}}]}
        with _patch('app.services.pagarme.requests.post',
                    return_value=_fake_resp(200, body)):
            loja_pagamento.iniciar_pix(ped)
        with _patch('app.services.tiny_nf.emitir_nf',
                    return_value={'ok': False, 'msg': 'Tiny fora'}), \
             _patch('app.services.email.enviar_nf_emitida') as mail:
            loja_pagamento.processar_webhook(
                {'id': 'evt_x', 'type': 'order.paid',
                 'data': {'id': 'or_x', 'code': ped.codigo}})
        mail.assert_not_called()
        atual = PedidoOnline.query.filter_by(codigo=ped.codigo).first()
        assert atual.status == 'pago'   # NF falhar não desfaz o pagamento


def test_order_paid_e_charge_paid_so_mandam_um_email(app):
    """Regressão (19/06/2026): o Pagar.me manda `order.paid` E `charge.paid`
    em sequência. Sem o lock pessimista no `_marcar_pago`, os dois eventos
    passavam pelo guard 'status == pago' (porque o segundo lia antes do
    commit do primeiro em paralelo) e mandavam 2 e-mails de 'pedido
    confirmado' (cliente Caio recebeu 2 e-mails idênticos)."""
    from unittest.mock import patch as _patch

    from app.extensions import db
    from app.models import PedidoOnline
    from app.services import loja_pagamento
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_abc'
        app.config['POSTMARK_SERVER_TOKEN'] = 'tok'
        loja = _loja_site(db)
        p = _produto(db)
        ped = _pedido_com_item(db, p, qtd=1, modo='retirada',
                                loja_retirada_id=loja.id)
        _setup_loja_estoque(db, ped, p)
        body = {'id': 'or_race', 'charges': [{'id': 'ch_race',
                'status': 'pending', 'last_transaction': {'qr_code': 'E'}}]}
        with _patch('app.services.pagarme.requests.post',
                    return_value=_fake_resp(200, body)):
            loja_pagamento.iniciar_pix(ped)
        with _patch('app.services.email.enviar_confirmacao_pedido',
                    return_value={'ok': True}) as envia:
            # order.paid chega primeiro
            loja_pagamento.processar_webhook(
                {'id': 'evt_order_race', 'type': 'order.paid',
                 'data': {'id': 'or_race', 'code': ped.codigo}})
            # charge.paid logo em seguida — DEVE ser no-op (sem 2º e-mail)
            loja_pagamento.processar_webhook(
                {'id': 'evt_charge_race', 'type': 'charge.paid',
                 'data': {'id': 'or_race', 'code': ped.codigo}})
        envia.assert_called_once()   # UM e-mail só, não dois
        atual = PedidoOnline.query.filter_by(codigo=ped.codigo).first()
        assert atual.status == 'pago'


def test_nf_que_levanta_excecao_nao_suja_a_sessao(app):
    """Regressão (19/06/2026): a emissão de NF rodava DENTRO da transação do
    pagamento e, ao falhar, deixava a sessão suja → poluía o teste/request
    seguinte (suite ficou flaky com 34 falhas). Agora a NF roda DEPOIS do
    commit, com rollback próprio: se levantar, o pedido segue pago E a sessão
    fica limpa (dá pra consultar logo em seguida)."""
    from unittest.mock import patch as _patch

    from app.extensions import db
    from app.models import PedidoOnline
    from app.services import loja_pagamento
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_abc'
        loja = _loja_site(db)
        p = _produto(db)
        ped = _pedido_com_item(db, p, qtd=1, modo='retirada',
                                loja_retirada_id=loja.id)
        _setup_loja_estoque(db, ped, p, qtd_atual=10)
        body = {'id': 'or_z', 'charges': [{'id': 'ch_z', 'status': 'pending',
                'last_transaction': {'qr_code': 'E'}}]}
        with _patch('app.services.pagarme.requests.post',
                    return_value=_fake_resp(200, body)):
            loja_pagamento.iniciar_pix(ped)
        with _patch('app.services.tiny_nf.emitir_nf',
                    side_effect=RuntimeError('Tiny explodiu')):
            res = loja_pagamento.processar_webhook(
                {'id': 'evt_z', 'type': 'order.paid',
                 'data': {'id': 'or_z', 'code': ped.codigo}})
        assert res.get('pago') is True
        # Sessão limpa: esta consulta NÃO pode estourar PendingRollbackError.
        atual = PedidoOnline.query.filter_by(codigo=ped.codigo).first()
        assert atual.status == 'pago'
        # E dá pra escrever também (sessão saudável).
        atual.nome_cliente = 'Teste Sessao'
        db.session.commit()


def test_email_nf_tem_link_da_danfe(app):
    """O e-mail dedicado da NF aponta pra rota pública /loja/pedido/<cod>/nf."""
    from types import SimpleNamespace

    from app.services.email import _template_nf, _texto_nf
    pedido = SimpleNamespace(codigo='ABC123', tiny_nota_fiscal_id='nf-9',
                             email_cliente='c@x.com')
    base = 'https://opao.online'
    html = _template_nf(pedido, base)
    txt = _texto_nf(pedido, base)
    link = f'{base}/loja/pedido/ABC123/nf'
    assert link in html and link in txt
    assert 'nf-9' in html


def test_reembolsar_pedido_estorna(app):
    from unittest.mock import patch as _patch

    from app.extensions import db
    from app.models import MovEstoqueLoja
    from app.services import loja_pagamento
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_abc'
        loja = _loja_site(db)
        p = _produto(db)
        ped = _pedido_com_item(db, p, qtd=2, modo='retirada',
                                loja_retirada_id=loja.id)
        el = _setup_loja_estoque(db, ped, p, qtd_atual=10)
        body = {'id': 'or_rf', 'charges': [{'id': 'ch_rf', 'status': 'pending',
                'last_transaction': {'qr_code': 'E'}}]}
        with _patch('app.services.pagarme.requests.post',
                    return_value=_fake_resp(200, body)):
            loja_pagamento.iniciar_pix(ped)
        loja_pagamento.processar_webhook(
            {'id': 'evt_p', 'type': 'order.paid',
             'data': {'id': 'or_rf', 'code': ped.codigo}})
        db.session.refresh(el)
        assert el.quantidade == 8  # baixou 2
        # Reembolso: cancela charge no gateway + estorna estoque
        with _patch('app.services.pagarme.cancelar_charge',
                    return_value={'ok': True}) as canc:
            ok, msg = loja_pagamento.reembolsar_pedido(ped)
        assert ok is True
        canc.assert_called_once_with('ch_rf')
        db.session.refresh(ped)
        db.session.refresh(el)
        assert ped.status == 'cancelado'
        assert el.quantidade == 10  # devolvido
        assert MovEstoqueLoja.query.filter_by(tipo='venda_site_estorno').count() == 1


def test_email_confirmacao_monta_resumo(app):
    from app.extensions import db
    from app.services import email as email_svc
    with app.app_context():
        app.config['POSTMARK_SERVER_TOKEN'] = 'tok'
        app.config['APP_BASE_URL'] = 'https://x'
        loja = _loja_site(db)
        p = _produto(db, preco=8.0)
        ped = _pedido_com_item(db, p, qtd=1, modo='retirada',
                                loja_retirada_id=loja.id)
        with patch('app.services.email.requests.post',
                   return_value=_fake_resp_email()) as post:
            r = email_svc.enviar_confirmacao_pedido(ped)
        assert r['ok'] is True
        html = post.call_args[1]['json']['HtmlBody']
        assert ped.codigo in html
        assert 'Box Mimo' in html


def _fake_resp_email():
    class R:
        status_code = 200
        text = ''
        def json(self):
            return {'MessageID': 'm1', 'ErrorCode': 0, 'Message': 'OK'}
    return R()


def test_pagamento_tela_renderiza(app, monkeypatch):
    from app.extensions import db
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin(app)
    _loja_site(db)
    p = _produto(db)
    ped = _pedido_com_item(db, p)
    r = c.get(f'/loja/pedido/{ped.codigo}/pagamento')
    assert r.status_code == 200
    assert b'Pix' in r.data


def test_status_json(app, monkeypatch):
    from app.extensions import db
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin(app)
    p = _produto(db)
    ped = _pedido_com_item(db, p)
    r = c.get(f'/loja/pedido/{ped.codigo}/status')
    assert r.status_code == 200
    assert r.get_json()['status'] == 'aguardando_pagamento'


def test_webhook_sem_segredo_503(app):
    c = app.test_client()
    app.config['PAGARME_WEBHOOK_SECRET'] = ''
    r = c.post('/loja/webhook/pagarme', json={'id': 'x', 'type': 'order.paid'})
    assert r.status_code == 503


def test_webhook_segredo_errado_401(app):
    c = app.test_client()
    app.config['PAGARME_WEBHOOK_SECRET'] = 'segredo_real_123'
    r = c.post('/loja/webhook/pagarme?k=errado',
               json={'id': 'x', 'type': 'order.paid'})
    assert r.status_code == 401


def test_webhook_segredo_correto_processa(app):
    from app.extensions import db
    c = app.test_client()
    app.config['PAGARME_WEBHOOK_SECRET'] = 'segredo_real_123'
    loja = _loja_site(db)
    p = _produto(db)
    ped = _pedido_com_item(db, p, modo='retirada', loja_retirada_id=loja.id)
    _setup_loja_estoque(db, ped, p)
    r = c.post('/loja/webhook/pagarme?k=segredo_real_123',
               json={'id': 'evt_route', 'type': 'order.paid',
                     'data': {'code': ped.codigo}})
    assert r.status_code == 200


def test_checkout_redireciona_pra_pagamento(app, monkeypatch):
    """O checkout (Fase 3) agora aponta pra a tela de pagamento (Fase 4)."""
    import json as _json

    from app.extensions import db
    from app.services import loja_checkout
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin(app)
    p = _produto(db, preco=25.0)
    from app.models import Loja
    db.session.add(Loja(nome='Brooklin', ativa=True, endereco='r'))
    db.session.commit()
    loja = Loja.query.filter_by(nome='Brooklin').first()
    data = loja_checkout.datas_disponiveis('retirada')[-1].isoformat()
    r = c.post('/loja/checkout', data={
        'nome': 'João', 'email': 'j@x.com', 'cpf': '52998224725',
        'aceite_lgpd': '1',
        'modo_entrega': 'retirada', 'loja_id': str(loja.id),
        'data_entrega': data, 'janela_entrega': '08:00–09:00',
        'itens_json': _json.dumps([{'kind': 'produto', 'id': p.id, 'qtd': 1}]),
    }, follow_redirects=False)
    assert r.status_code == 302
    assert '/pagamento' in r.headers['Location']

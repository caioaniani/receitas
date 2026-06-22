"""Link de pagamento hospedado (Apple Pay + Pix + cartao na pagina do
Pagar.me). Adicionado 22/06/2026 ("link agora, nativo depois").

Cobre:
- pagarme.criar_link_pagamento monta Order com payment_method=checkout e
  extrai a payment_url;
- loja_pagamento.iniciar_link cria PagamentoOnline(metodo='link') + guarda
  order_id + devolve a URL;
- o webhook 'order.paid' de um pedido pago via link marca pago e baixa
  estoque (consome reserva) pelo MESMO caminho do Pix/cartao.
"""
from decimal import Decimal
from unittest.mock import patch


def _site_loja(db):
    from app.models import AppConfig, Loja
    loja = Loja(nome='Loja do Site', ativa=True, endereco='Rua Site, 1')
    db.session.add(loja)
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', loja.id)
    db.session.commit()
    return loja


def _produto(db, nome='Pao', preco=10.0):
    from app.models import Produto
    p = Produto(nome=nome, categoria='Paes', preco_site=preco,
                imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


def _pedido(db, *, codigo='LINK0001', loja=None, prod=None, qtd=2):
    from app.models import Cliente, PedidoOnline, PedidoOnlineItem
    cli = Cliente(nome='Maria', email=f'{codigo.lower()}@x.com')
    db.session.add(cli)
    db.session.commit()
    p = PedidoOnline(
        codigo=codigo, cliente_id=cli.id, nome_cliente='Maria',
        email_cliente=cli.email, modo_entrega='retirada',
        loja_retirada_id=(loja.id if loja else None),
        status='aguardando_pagamento',
        subtotal=Decimal('0'), frete_valor=Decimal('0'),
        valor_total=Decimal('0'),
    )
    db.session.add(p)
    db.session.flush()
    if prod:
        p.itens.append(PedidoOnlineItem(
            kind='produto', produto_id=prod.id, nome=prod.nome,
            preco_unitario=Decimal(str(prod.preco_site)), quantidade=qtd,
            subtotal=Decimal(str(prod.preco_site)) * qtd))
    p.recalcular_total()
    db.session.commit()
    return p


def test_criar_link_monta_checkout_e_extrai_url(app):
    from app.services import pagarme
    with app.app_context():
        from app.extensions import db
        loja = _site_loja(db)
        prod = _produto(db, 'Pao Link', 25.0)
        ped = _pedido(db, loja=loja, prod=prod, qtd=2)

        fake = (201, {
            'id': 'or_ABC', 'code': ped.codigo,
            'checkouts': [{'id': 'chk_1',
                           'payment_url': 'https://pag.link/or_ABC'}],
        })
        with patch('app.services.pagarme._post_order', return_value=fake) as m:
            res = pagarme.criar_link_pagamento(
                ped, success_url='https://loja/x', expira_em_min=60)

        assert res['ok'] is True
        assert res['payment_url'] == 'https://pag.link/or_ABC'
        assert res['order_id'] == 'or_ABC'
        # confere o payload enviado
        payload = m.call_args[0][0]
        pay = payload['payments'][0]
        assert pay['payment_method'] == 'checkout'
        assert payload['code'] == ped.codigo
        assert 'credit_card' in pay['checkout']['accepted_payment_methods']
        assert 'pix' in pay['checkout']['accepted_payment_methods']
        assert pay['checkout']['success_url'] == 'https://loja/x'


def test_criar_link_sem_url_falha(app):
    from app.services import pagarme
    with app.app_context():
        from app.extensions import db
        ped = _pedido(db, prod=_produto(db))
        # Pagar.me respondeu 201 mas sem checkouts/payment_url.
        with patch('app.services.pagarme._post_order',
                   return_value=(201, {'id': 'or_X'})):
            res = pagarme.criar_link_pagamento(ped)
        assert res['ok'] is False
        assert 'link' in res['erro'].lower()


def test_iniciar_link_cria_pagamento_e_guarda_order(app):
    from app.services import loja_pagamento
    with app.app_context():
        from app.extensions import db
        from app.models import PagamentoOnline
        loja = _site_loja(db)
        ped = _pedido(db, loja=loja, prod=_produto(db))
        with patch('app.services.pagarme.criar_link_pagamento',
                   return_value={'ok': True, 'order_id': 'or_9',
                                 'payment_url': 'https://pag.link/or_9'}):
            url, erros = loja_pagamento.iniciar_link(
                ped, success_url='https://loja/status')
        assert erros == []
        assert url == 'https://pag.link/or_9'
        pag = PagamentoOnline.query.filter_by(pedido_id=ped.id).first()
        assert pag.metodo == 'link'
        assert pag.pagarme_order_id == 'or_9'


def test_iniciar_link_falha_marca_pagamento(app):
    from app.services import loja_pagamento
    with app.app_context():
        from app.extensions import db
        from app.models import PagamentoOnline
        ped = _pedido(db, prod=_produto(db))
        with patch('app.services.pagarme.criar_link_pagamento',
                   return_value={'ok': False, 'erro': 'boom'}):
            url, erros = loja_pagamento.iniciar_link(ped)
        assert url is None
        assert erros
        pag = PagamentoOnline.query.filter_by(pedido_id=ped.id).first()
        assert pag.status == 'falhou'


def test_webhook_paga_pedido_via_link_baixa_estoque(app):
    """Pedido pago via link: webhook casa por order_id e baixa estoque."""
    from app.services import loja_pagamento
    with app.app_context():
        from app.extensions import db
        from app.models import EstoqueLoja, MovEstoqueLoja
        loja = _site_loja(db)
        prod = _produto(db, 'Pao WH', 30.0)
        el = EstoqueLoja(loja_id=loja.id, produto_id=prod.id,
                         quantidade=10, quantidade_reservada=0)
        db.session.add(el)
        db.session.commit()
        ped = _pedido(db, loja=loja, prod=prod, qtd=2)
        # reserva (como o checkout faz)
        from app.services import loja_estoque_reserva
        loja_estoque_reserva.reservar(ped, loja_id=loja.id)
        db.session.commit()

        # cria o pagamento via link (mock do Pagar.me)
        with patch('app.services.pagarme.criar_link_pagamento',
                   return_value={'ok': True, 'order_id': 'or_WH',
                                 'payment_url': 'https://pag.link/or_WH'}):
            loja_pagamento.iniciar_link(ped)

        # webhook 'order.paid' chega com o order_id
        tratado = loja_pagamento.processar_webhook(
            'order.paid', {'id': 'or_WH', 'code': ped.codigo,
                           'status': 'paid'})
        assert tratado is True
        db.session.refresh(ped)
        assert ped.status == 'pago'
        db.session.refresh(el)
        # baixou 2 (consumiu reserva): 10 -> 8, reservada volta a 0
        assert el.quantidade == 8
        assert el.quantidade_reservada == 0
        movs = MovEstoqueLoja.query.filter_by(
            estoque_loja_id=el.id, tipo='venda_site').count()
        assert movs == 1

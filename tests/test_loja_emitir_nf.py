"""Emissão de NF via Tiny (Fase 5 — plano A, botão manual).

Cobre orquestração emitir_nf: idempotência, ordem de chamadas, payload por
SKU mapeado, falha quando item sem SKU. NÃO chama o Tiny real (mockado).
"""
from decimal import Decimal
from unittest.mock import patch


def _owner(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Dono', login='dono', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def _produto(db, nome='Box Mimo', preco=20.0):
    from app.models import Produto
    p = Produto(nome=nome, categoria='Cestas', preco_site=preco,
                imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


def _pedido_pago(db, produto, qtd=1, sku=None):
    from app.models import Cliente, PedidoOnline, PedidoOnlineItem
    from app.services import tiny_nf
    if sku:
        tiny_nf.definir_sku('produto', produto.id, sku)
    cli = Cliente(nome='Maria', email='m@x.com', cpf='52998224725',
                  telefone='11999999999')
    db.session.add(cli)
    db.session.flush()
    p = PedidoOnline(cliente_id=cli.id, nome_cliente='Maria',
                     email_cliente='m@x.com', telefone_cliente='11999999999',
                     modo_entrega='retirada', status='pago',
                     subtotal=Decimal(str(produto.preco_site)) * qtd,
                     frete_valor=Decimal('0'),
                     valor_total=Decimal(str(produto.preco_site)) * qtd)
    db.session.add(p)
    db.session.flush()
    p.itens.append(PedidoOnlineItem(
        kind='produto', produto_id=produto.id, nome=produto.nome,
        preco_unitario=Decimal(str(produto.preco_site)), quantidade=qtd,
        subtotal=Decimal(str(produto.preco_site)) * qtd))
    db.session.commit()
    return p


def test_emitir_nf_ordem_e_payload(app):
    """Chama incluir_pedido -> gerar_nota_fiscal_pedido -> emitir_nota_fiscal
    nessa ordem, com o SKU mapeado no payload."""
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db, preco=20.0)
        p = _pedido_pago(db, produto, qtd=2, sku='SKU-XYZ')
        with patch('app.services.tiny.incluir_pedido',
                   return_value={'ok': True, 'id': 'tp-1', 'numero': '999'}) as inc, \
             patch('app.services.tiny.gerar_nota_fiscal_pedido',
                   return_value={'ok': True, 'id_nota_fiscal': 'nf-9', 'status': 'aberta'}) as ger, \
             patch('app.services.tiny.emitir_nota_fiscal',
                   return_value={'ok': True, 'status': 'autorizada'}) as emi:
            res = tiny_nf.emitir_nf(p)
        assert res['ok'] and res['nota_fiscal_id'] == 'nf-9'
        # Payload do pedido carrega o SKU + numero_ordem_compra = codigo nosso
        ped_payload = inc.call_args[0][0]
        assert ped_payload['numero_ordem_compra'] == p.codigo
        assert ped_payload['itens'][0]['item']['codigo'] == 'SKU-XYZ'
        assert ped_payload['itens'][0]['item']['quantidade'] == 2.0
        ger.assert_called_once_with('tp-1')
        emi.assert_called_once_with('nf-9')
        # Persistiu
        db.session.refresh(p)
        assert p.tiny_pedido_id == 'tp-1'
        assert p.tiny_nota_fiscal_id == 'nf-9'
        assert p.nf_emitida_em is not None


def test_emitir_nf_idempotente(app):
    """Pedido que já tem nota_fiscal_id NÃO chama o Tiny de novo."""
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db)
        p = _pedido_pago(db, produto, sku='SKU')
        p.tiny_nota_fiscal_id = 'nf-existente'
        db.session.commit()
        with patch('app.services.tiny.incluir_pedido') as inc:
            res = tiny_nf.emitir_nf(p)
        assert res['ok'] and res['nota_fiscal_id'] == 'nf-existente'
        inc.assert_not_called()


def test_emitir_nf_bloqueia_sem_sku(app):
    """Item sem SKU mapeado: aborta com mensagem clara, NÃO emite parcial."""
    from app.extensions import db
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db, nome='Sem Mapeamento')
        p = _pedido_pago(db, produto)   # sku=None
        with patch('app.services.tiny.incluir_pedido') as inc:
            res = tiny_nf.emitir_nf(p)
        assert res['ok'] is False
        assert 'Sem Mapeamento' in res['msg']
        inc.assert_not_called()


def test_emitir_nf_bloqueia_se_nao_pago(app):
    from app.extensions import db
    from app.models import PedidoOnline
    from app.services import tiny_nf
    with app.app_context():
        produto = _produto(db)
        p = _pedido_pago(db, produto, sku='S')
        p.status = 'aguardando_pagamento'
        db.session.commit()
        assert tiny_nf.emitir_nf(p)['ok'] is False
        # Não criou nada no Tiny
        assert PedidoOnline.query.first().tiny_nota_fiscal_id is None


def test_botao_emitir_nf_no_admin(app):
    from app.extensions import db
    c = _owner(app)
    produto = _produto(db)
    p = _pedido_pago(db, produto, sku='S')
    with patch('app.services.tiny.incluir_pedido',
               return_value={'ok': True, 'id': 'tp', 'numero': '1'}), \
         patch('app.services.tiny.gerar_nota_fiscal_pedido',
               return_value={'ok': True, 'id_nota_fiscal': 'nf', 'status': 'ok'}), \
         patch('app.services.tiny.emitir_nota_fiscal',
               return_value={'ok': True, 'status': 'autorizada'}):
        r = c.post(f'/admin/loja-online/pedidos/{p.codigo}/emitir-nf',
                   follow_redirects=False)
    assert r.status_code == 302
    with app.app_context():
        from app.models import PedidoOnline
        atual = PedidoOnline.query.filter_by(codigo=p.codigo).first()
        assert atual.tiny_nota_fiscal_id == 'nf'


def test_incluir_pedido_registros_como_dict(app):
    """Regressão do 500 (KeyError: 0): Tiny v2 às vezes manda `registros`
    como DICT {'registro': {...}}, não lista. _registros normaliza."""
    from app.services import tiny
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'tok'

        class R:
            status_code = 200
            def json(self):
                return {'retorno': {'status': 'OK', 'registros': {
                    'registro': {'id': '777', 'numero': '5', 'status': 'OK'}}}}
        with patch('app.services.tiny.requests.post', return_value=R()):
            res = tiny.incluir_pedido({'cliente': {}, 'itens': []})
        assert res['ok'] is True and res['id'] == '777'


def test_incluir_pedido_erro_propaga_mensagem(app):
    """Tiny recusa: a mensagem real volta no 'erro' (não 'ver logs')."""
    from app.services import tiny
    with app.app_context():
        app.config['TINY_API_TOKEN'] = 'tok'

        class R:
            status_code = 200
            def json(self):
                return {'retorno': {'status': 'Erro', 'registros': {
                    'registro': {'erros': [{'erro': 'Cliente sem endereço'}]}}}}
        with patch('app.services.tiny.requests.post', return_value=R()):
            res = tiny.incluir_pedido({'cliente': {}, 'itens': []})
        assert res['ok'] is False
        assert 'endereço' in res['erro']


def test_detalhe_mostra_botao_emitir_pra_pago(app):
    from app.extensions import db
    c = _owner(app)
    produto = _produto(db)
    p = _pedido_pago(db, produto, sku='S')
    r = c.get(f'/admin/loja-online/pedidos/{p.codigo}')
    assert r.status_code == 200
    assert b'Emitir NF' in r.data

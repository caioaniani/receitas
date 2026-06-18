"""E-mails transacionais da loja online (Fase 6 — PRs 2-4).

- "Recebemos seu pedido" (checkout) ✓
- "Pagamento confirmado" (já existia; cobertura básica)
- "Saiu pra entrega" (status `a_caminho`)

Postmark é mockado — testa que disparamos a função certa, não o envio HTTP.
"""
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch


def _form(**kw):
    base = {'nome': 'Maria', 'email': 'maria@x.com', 'telefone': '11999',
            'cpf': '52998224725', 'aceite_lgpd': '1'}
    base.setdefault('logradouro', 'Rua X')
    base.setdefault('numero', '10')
    base.setdefault('bairro', 'Moema')
    base.setdefault('cidade', 'São Paulo')
    base.setdefault('uf', 'SP')
    base.setdefault('cep', '04077000')
    base.update(kw)
    return base


def _produto(db, preco=20.0):
    from app.models import Produto
    p = Produto(nome='Box', categoria='Cestas', preco_site=preco,
                imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


def _loja(db):
    from app.models import Loja
    loja = Loja(nome='Brooklin', endereco='Ribeiro do Vale, 455', ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


def test_checkout_dispara_email_recebido(app):
    """Pedido criado no checkout → enviar_pedido_recebido é chamado."""
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db, preco=20.0)
        loja = _loja(db)
        base_dt = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis(
            'retirada', base=base_dt)[1].isoformat()
        form = _form(modo_entrega='retirada', loja_id=str(loja.id),
                     data_entrega=data, janela_entrega='08:00–09:00')
        with patch('app.services.email.disponivel', return_value=True), \
             patch('app.services.email.enviar_pedido_recebido') as ev:
            pedido, erros = loja_checkout.criar_pedido(
                form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}],
                base=base_dt)
        assert erros == []
        ev.assert_called_once()
        # Confere que mandou o pedido completo (com código)
        chamado_com = ev.call_args[0][0]
        assert chamado_com.codigo == pedido.codigo


def test_checkout_email_falhando_nao_derruba_pedido(app):
    """Email falhar NÃO pode quebrar o checkout (best-effort)."""
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        p = _produto(db, preco=20.0)
        loja = _loja(db)
        base_dt = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis(
            'retirada', base=base_dt)[1].isoformat()
        form = _form(modo_entrega='retirada', loja_id=str(loja.id),
                     data_entrega=data, janela_entrega='08:00–09:00')
        with patch('app.services.email.disponivel', return_value=True), \
             patch('app.services.email.enviar_pedido_recebido',
                   side_effect=RuntimeError('Postmark down')):
            pedido, erros = loja_checkout.criar_pedido(
                form, [{'kind': 'produto', 'id': p.id, 'qtd': 1}],
                base=base_dt)
        assert erros == []
        assert pedido is not None and pedido.codigo


def test_email_recebido_tem_assunto_e_link_pagamento(app):
    """O assunto contém o código + corpo tem link de pagamento."""
    from app.services import email as email_svc
    with app.app_context():
        app.config['APP_BASE_URL'] = 'https://example.com'
        app.config['POSTMARK_SERVER_TOKEN'] = 'tok'

        class FakePedido:
            codigo = 'XYZ123'
            email_cliente = 'c@x.com'
            subtotal = Decimal('20')
            frete_valor = Decimal('5')
            valor_total = Decimal('25')
            modo_entrega = 'retirada'
            loja_retirada = None
            endereco_entrega = None
            data_entrega = None
            janela_entrega = None
            itens = []

        with patch('app.services.email.enviar',
                   return_value={'ok': True}) as enviar:
            email_svc.enviar_pedido_recebido(FakePedido())
        args, kwargs = enviar.call_args
        destinatario, assunto, html = args[:3]
        assert 'XYZ123' in assunto
        assert 'pagamento' in html.lower()


def test_email_a_caminho_renderiza(app):
    """A função existe e monta um HTML com 'caminho'."""
    from app.services import email as email_svc
    with app.app_context():
        app.config['APP_BASE_URL'] = 'https://example.com'
        app.config['POSTMARK_SERVER_TOKEN'] = 'tok'

        class FakePedido:
            codigo = 'XYZ456'
            email_cliente = 'c@x.com'
            modo_entrega = 'agendada'
            loja_retirada = None
            endereco_entrega = 'Rua A, 1'
            data_entrega = None
            janela_entrega = None
            itens = []

        with patch('app.services.email.enviar',
                   return_value={'ok': True}) as enviar:
            email_svc.enviar_pedido_a_caminho(FakePedido())
        args, _ = enviar.call_args
        _, assunto, html = args[:3]
        assert 'caminho' in assunto.lower()
        assert 'XYZ456' in html

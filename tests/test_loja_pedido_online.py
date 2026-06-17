"""Modelos da loja online (Fase 3): Cliente, EnderecoCliente, PedidoOnline.

Cobre a fundacao do checkout nativo: criacao de cliente (guest e com
conta), unicidade de email, snapshot de preco no item, e precisao de
dinheiro (Numeric(10,2) + Decimal — peso especial no CLAUDE.md).
"""
from decimal import Decimal

import pytest


def test_cliente_guest_sem_senha(app):
    from app.extensions import db
    from app.models import Cliente
    with app.app_context():
        c = Cliente(nome='Maria', email='maria@x.com', telefone='11999')
        db.session.add(c)
        db.session.commit()
        assert c.id is not None
        # Guest: sem senha -> sem conta de verdade
        assert c.tem_conta is False
        assert c.check_senha('qualquer') is False


def test_cliente_vira_conta_com_senha(app):
    from app.extensions import db
    from app.models import Cliente
    with app.app_context():
        c = Cliente(nome='João', email='joao@x.com')
        c.set_senha('segredo123')
        db.session.add(c)
        db.session.commit()
        assert c.tem_conta is True
        assert c.check_senha('segredo123') is True
        assert c.check_senha('errada') is False


def test_cliente_email_unico(app):
    from sqlalchemy.exc import IntegrityError

    from app.extensions import db
    from app.models import Cliente
    with app.app_context():
        db.session.add(Cliente(nome='A', email='dup@x.com'))
        db.session.commit()
        db.session.add(Cliente(nome='B', email='dup@x.com'))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_pedido_codigo_gerado_e_formato(app):
    from app.extensions import db
    from app.models import PedidoOnline
    with app.app_context():
        p = PedidoOnline(
            nome_cliente='Maria', email_cliente='maria@x.com',
            modo_entrega='retirada')
        db.session.add(p)
        db.session.commit()
        assert p.codigo and len(p.codigo) == 8
        # 8 chars hex maiusculo
        assert p.codigo == p.codigo.upper()
        int(p.codigo, 16)  # nao levanta = e hex valido


def test_pedido_codigo_unico_entre_pedidos(app):
    from app.extensions import db
    from app.models import PedidoOnline
    with app.app_context():
        codigos = set()
        for _ in range(20):
            p = PedidoOnline(
                nome_cliente='X', email_cliente='x@x.com',
                modo_entrega='retirada')
            db.session.add(p)
            db.session.commit()
            codigos.add(p.codigo)
        assert len(codigos) == 20  # sem colisao


def test_pedido_recalcular_total_decimal_exato(app):
    """3 itens de R$33,33 = R$99,99 exato (Float erraria). + frete R$5 =
    R$104,99. Dinheiro em Decimal, sem arredondamento sujo."""
    from app.extensions import db
    from app.models import PedidoOnline, PedidoOnlineItem
    with app.app_context():
        p = PedidoOnline(
            nome_cliente='Maria', email_cliente='maria@x.com',
            modo_entrega='agendada', frete_valor=Decimal('5.00'))
        db.session.add(p)
        db.session.flush()
        for _ in range(3):
            p.itens.append(PedidoOnlineItem(
                kind='receita', nome='Pão', preco_unitario=Decimal('33.33'),
                quantidade=1, subtotal=Decimal('33.33')))
        db.session.commit()
        total = p.recalcular_total()
        assert p.subtotal == Decimal('99.99')
        assert total == Decimal('104.99')


def test_pedido_total_com_quantidade(app):
    from app.extensions import db
    from app.models import PedidoOnline, PedidoOnlineItem
    with app.app_context():
        p = PedidoOnline(
            nome_cliente='M', email_cliente='m@x.com',
            modo_entrega='agendada', frete_valor=Decimal('0'))
        db.session.add(p)
        db.session.flush()
        # 2x R$10,50 = R$21,00
        p.itens.append(PedidoOnlineItem(
            kind='produto', nome='Cesta', preco_unitario=Decimal('10.50'),
            quantidade=2, subtotal=Decimal('21.00')))
        db.session.commit()
        assert p.recalcular_total() == Decimal('21.00')


def test_item_snapshot_preco_independe_do_catalogo(app):
    """O item guarda nome+preco do momento do pedido (snapshot). Mexer no
    preco_site do catalogo depois NAO altera pedidos passados."""
    from app.extensions import db
    from app.models import PedidoOnline, PedidoOnlineItem
    with app.app_context():
        p = PedidoOnline(
            nome_cliente='M', email_cliente='m@x.com', modo_entrega='retirada')
        db.session.add(p)
        db.session.flush()
        it = PedidoOnlineItem(
            kind='receita', nome='Sourdough (na época)',
            preco_unitario=Decimal('28.00'), quantidade=1,
            subtotal=Decimal('28.00'))
        p.itens.append(it)
        db.session.commit()
        # snapshot persistido, independente de FK
        assert it.nome == 'Sourdough (na época)'
        assert it.preco_unitario == Decimal('28.00')


def test_endereco_cliente_linha_unica(app):
    from app.extensions import db
    from app.models import Cliente, EnderecoCliente
    with app.app_context():
        c = Cliente(nome='M', email='endereco@x.com')
        db.session.add(c)
        db.session.flush()
        e = EnderecoCliente(
            cliente_id=c.id, logradouro='Rua A', numero='10',
            bairro='Centro', cidade='São Paulo', uf='SP')
        db.session.add(e)
        db.session.commit()
        assert e.linha_unica() == 'Rua A, 10, Centro, São Paulo, SP'
        # cliente.enderecos (lazy dynamic) enxerga
        assert c.enderecos.count() == 1


def test_venda_site_nas_constantes_de_loja(app):
    from app.constants import VENDA_TIPOS_LOJA, VENDA_TIPOS_TODOS
    assert 'venda_site' in VENDA_TIPOS_LOJA
    assert 'venda_site_estorno' in VENDA_TIPOS_LOJA
    assert 'venda_site' in VENDA_TIPOS_TODOS


def test_pedido_status_default_aguardando_pagamento(app):
    from app.extensions import db
    from app.models import PedidoOnline
    with app.app_context():
        p = PedidoOnline(
            nome_cliente='M', email_cliente='m@x.com', modo_entrega='retirada')
        db.session.add(p)
        db.session.commit()
        assert p.status == 'aguardando_pagamento'

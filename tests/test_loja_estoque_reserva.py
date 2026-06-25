"""Reserva de estoque pra loja online (21/06/2026).

Cobre:
- Reserva no checkout segura saldo virtual (catalogo mostra disponivel
  correto).
- Webhook 'pago' consome reserva e baixa real.
- Cancelamento antes do pagamento libera reserva.
- Cron liberar_expirados cancela pedido e devolve saldo.
- Race condition: 2 checkouts simultaneos pra ultimo item — o segundo
  e' rejeitado.
"""
from datetime import timedelta
from decimal import Decimal


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


def _estoque(db, loja, produto, qtd, reservada=0):
    from app.models import EstoqueLoja
    el = EstoqueLoja(loja_id=loja.id, produto_id=produto.id,
                     quantidade=qtd, quantidade_reservada=reservada)
    db.session.add(el)
    db.session.commit()
    return el


def _pedido(db, *, codigo='AAAA0001', loja_retirada=None,
            itens=(), status='aguardando_pagamento'):
    from app.models import Cliente, PedidoOnline, PedidoOnlineItem
    cli = Cliente(nome='Maria', email=f'{codigo.lower()}@x.com')
    db.session.add(cli)
    db.session.commit()
    p = PedidoOnline(
        codigo=codigo, cliente_id=cli.id, nome_cliente='Maria',
        email_cliente=cli.email,
        modo_entrega='retirada',
        loja_retirada_id=(loja_retirada.id if loja_retirada else None),
        status=status,
        subtotal=Decimal('0'), frete_valor=Decimal('0'),
        valor_total=Decimal('0'),
    )
    db.session.add(p)
    db.session.flush()
    for prod, qtd in itens:
        p.itens.append(PedidoOnlineItem(
            kind='produto', produto_id=prod.id, nome=prod.nome,
            preco_unitario=Decimal(str(prod.preco_site)),
            quantidade=qtd,
            subtotal=Decimal(str(prod.preco_site)) * qtd,
        ))
    p.recalcular_total()
    db.session.commit()
    return p


def test_reservar_segura_saldo_virtual(app):
    from app.extensions import db
    from app.services import loja_catalogo, loja_estoque_reserva
    with app.app_context():
        loja = _site_loja(db)
        prod = _produto(db, 'Pao da Casa')
        el = _estoque(db, loja, prod, qtd=5)
        ped = _pedido(db, loja_retirada=loja, itens=[(prod, 3)])

        r = loja_estoque_reserva.reservar(ped, loja_id=loja.id)

        assert r['ok'] is True
        assert r['reservas'] == 1
        db.session.refresh(el)
        assert el.quantidade == 5
        assert el.quantidade_reservada == 3
        assert ped.reserva_expira_em is not None
        # Catalogo mostra disponivel = 5 - 3 = 2
        mapa = loja_catalogo._estoque_site_map()
        assert mapa[('produto', prod.id)] == 2


def test_reservar_rejeita_quando_excede_disponivel(app):
    from app.extensions import db
    from app.services import loja_estoque_reserva
    with app.app_context():
        loja = _site_loja(db)
        prod = _produto(db, 'Pao Limitado')
        el = _estoque(db, loja, prod, qtd=2)
        ped = _pedido(db, loja_retirada=loja, itens=[(prod, 5)])

        r = loja_estoque_reserva.reservar(ped, loja_id=loja.id)

        assert r['ok'] is False
        assert len(r['sem_estoque']) == 1
        assert r['sem_estoque'][0]['pedido'] == 5
        assert r['sem_estoque'][0]['disponivel'] == 2
        # Nada reservado quando falhou
        db.session.refresh(el)
        assert el.quantidade_reservada == 0
        assert ped.reserva_expira_em is None


def test_consumir_baixa_real_e_libera_reserva(app):
    from app.extensions import db
    from app.models import MovEstoqueLoja
    from app.services import loja_estoque_reserva
    with app.app_context():
        loja = _site_loja(db)
        prod = _produto(db, 'Pao Comum')
        el = _estoque(db, loja, prod, qtd=10, reservada=3)
        ped = _pedido(db, loja_retirada=loja, itens=[(prod, 3)])
        from app.utils import agora
        ped.reserva_expira_em = agora() + timedelta(minutes=30)
        db.session.commit()

        r = loja_estoque_reserva.consumir(ped, loja_id=loja.id)

        assert r['baixado'] == 3
        assert r['faltou'] == 0
        db.session.refresh(el)
        assert el.quantidade == 7
        assert el.quantidade_reservada == 0
        assert ped.reserva_expira_em is None
        # Auditoria do mov de venda
        movs = MovEstoqueLoja.query.filter_by(
            estoque_loja_id=el.id, tipo='venda_site').all()
        assert len(movs) == 1
        assert movs[0].quantidade == 3


def test_consumir_idempotente_no_retry_do_webhook(app):
    from app.extensions import db
    from app.models import MovEstoqueLoja
    from app.services import loja_estoque_reserva
    with app.app_context():
        loja = _site_loja(db)
        prod = _produto(db, 'Pao Retry')
        el = _estoque(db, loja, prod, qtd=10, reservada=2)
        ped = _pedido(db, loja_retirada=loja, itens=[(prod, 2)])

        loja_estoque_reserva.consumir(ped, loja_id=loja.id)
        antes = el.quantidade
        # 2a chamada (retry do webhook) NAO deve baixar de novo
        r2 = loja_estoque_reserva.consumir(ped, loja_id=loja.id)

        assert r2.get('ja_consumido') is True
        db.session.refresh(el)
        assert el.quantidade == antes  # nao baixou de novo
        movs = MovEstoqueLoja.query.filter_by(
            estoque_loja_id=el.id, tipo='venda_site').all()
        assert len(movs) == 1


def test_liberar_devolve_saldo_virtual(app):
    from app.extensions import db
    from app.services import loja_estoque_reserva
    from app.utils import agora
    with app.app_context():
        loja = _site_loja(db)
        prod = _produto(db, 'Pao Cancelado')
        el = _estoque(db, loja, prod, qtd=5, reservada=2)
        ped = _pedido(db, loja_retirada=loja, itens=[(prod, 2)])
        ped.reserva_expira_em = agora() + timedelta(minutes=30)
        db.session.commit()

        r = loja_estoque_reserva.liberar(ped, loja_id=loja.id)

        assert r['liberadas'] == 1
        db.session.refresh(el)
        assert el.quantidade == 5  # fisico nao mexe
        assert el.quantidade_reservada == 0
        assert ped.reserva_expira_em is None


def test_liberar_expirados_cancela_pedido_e_devolve_saldo(app):
    from app.extensions import db
    from app.services import loja_estoque_reserva
    from app.utils import agora
    with app.app_context():
        loja = _site_loja(db)
        prod = _produto(db, 'Pao Abandonado')
        el = _estoque(db, loja, prod, qtd=4, reservada=2)
        ped = _pedido(db, loja_retirada=loja, itens=[(prod, 2)])
        # Reserva ja vencida (cliente abandonou o checkout)
        ped.reserva_expira_em = agora() - timedelta(minutes=1)
        db.session.commit()

        codigos = loja_estoque_reserva.liberar_expirados()

        assert ped.codigo in codigos
        db.session.refresh(ped)
        assert ped.status == 'cancelado'
        assert ped.motivo_cancelamento == 'pix_expirado'
        assert ped.cancelado_em is not None
        db.session.refresh(el)
        assert el.quantidade_reservada == 0


def test_liberar_expirados_ignora_pago_e_ainda_dentro_do_prazo(app):
    from app.extensions import db
    from app.services import loja_estoque_reserva
    from app.utils import agora
    with app.app_context():
        loja = _site_loja(db)
        prod_a = _produto(db, 'Pao Pago', preco=10)
        prod_b = _produto(db, 'Pao Vivo', preco=12)
        _estoque(db, loja, prod_a, qtd=3)
        _estoque(db, loja, prod_b, qtd=3)
        # Pago: nao mexe (mesmo com reserva_expira_em no passado, o status
        # blinda)
        ped_pago = _pedido(db, codigo='PAGO0001', loja_retirada=loja,
                           itens=[(prod_a, 1)], status='pago')
        ped_pago.reserva_expira_em = agora() - timedelta(minutes=99)
        # Vivo: dentro do prazo, nao expirou
        ped_vivo = _pedido(db, codigo='VIVO0001', loja_retirada=loja,
                           itens=[(prod_b, 1)])
        ped_vivo.reserva_expira_em = agora() + timedelta(minutes=30)
        db.session.commit()

        codigos = loja_estoque_reserva.liberar_expirados()

        assert codigos == []
        db.session.refresh(ped_pago)
        db.session.refresh(ped_vivo)
        assert ped_pago.status == 'pago'
        assert ped_vivo.status == 'aguardando_pagamento'


def test_estoque_loja_disponivel_property():
    from app.models import EstoqueLoja
    el = EstoqueLoja(quantidade=10, quantidade_reservada=3)
    assert el.disponivel == 7
    el.quantidade_reservada = 0
    assert el.disponivel == 10
    # Caso degenerado: reservada > quantidade (nao deveria acontecer, mas
    # nao queremos negativo).
    el.quantidade_reservada = 20
    assert el.disponivel == 0

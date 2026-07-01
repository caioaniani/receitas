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


def test_reserva_fisica_nunca_bloqueia_por_falta(app):
    """Regra do dono (01/07/2026): o estoque fisico NAO decide a venda do site
    (isso e' o plano-do-dia). Pedir mais do que ha no fisico NAO barra o
    checkout — reserva best-effort (registra a demanda cheia; disponivel
    clampa em 0) e a baixa real no pagamento tolera o shortfall."""
    from app.extensions import db
    from app.services import loja_estoque_reserva
    with app.app_context():
        loja = _site_loja(db)
        prod = _produto(db, 'Pao Limitado')
        el = _estoque(db, loja, prod, qtd=2)
        ped = _pedido(db, loja_retirada=loja, itens=[(prod, 5)])

        r = loja_estoque_reserva.reservar(ped, loja_id=loja.id)

        assert r['ok'] is True                       # NAO bloqueia mais
        assert r['sem_estoque'] == []
        assert r['reservas'] == 1
        db.session.refresh(el)
        assert el.quantidade_reservada == 5          # reservou a demanda cheia
        assert el.disponivel == 0                    # clampa (2 - 5 -> 0)
        assert ped.reserva_expira_em is not None


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


def _receita(db, nome='Croissant'):
    from app.models import Receita
    r = Receita(nome=nome, categoria='Viennoiserie', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def _estoque_rec(db, loja, receita, qtd):
    from app.models import EstoqueLoja
    el = EstoqueLoja(loja_id=loja.id, receita_id=receita.id, quantidade=qtd)
    db.session.add(el)
    db.session.commit()
    return el


def _cesta(db, nome, componentes):
    """componentes: [(receita_or_None, item_nome, qtd_por_cesta)]."""
    from app.models import Produto, ProdutoItem
    cesta = Produto(nome=nome, categoria='Cestas', preco_site=100.0,
                    imagem_dropbox_url='https://x/c.jpg', ativo=True)
    db.session.add(cesta)
    db.session.flush()
    for rec, item_nome, qtd in componentes:
        db.session.add(ProdutoItem(
            produto_id=cesta.id, tipo='receita',
            receita_id=(rec.id if rec else None),
            item_nome=item_nome, quantidade=qtd))
    db.session.commit()
    return cesta


def test_cesta_baixa_componentes_nao_a_cesta(app):
    """Vender uma cesta no site baixa CADA componente rastreado (× qtd da
    cesta × qtd comprada), NAO a cesta. Componente sem linha de estoque
    (decorativo) e ignorado — nao bloqueia nem inventa linha."""
    from app.extensions import db
    from app.models import EstoqueLoja, MovEstoqueLoja
    from app.services import loja_estoque_reserva
    from app.utils import agora
    with app.app_context():
        loja = _site_loja(db)
        croissant = _receita(db, 'Croissant Tradicional')
        cookie = _receita(db, 'Cookie Calebaut')
        deco = _receita(db, 'Arranjo de Flor')      # SEM linha de estoque
        _estoque_rec(db, loja, croissant, qtd=10)
        _estoque_rec(db, loja, cookie, qtd=10)
        cesta = _cesta(db, 'Family Box', [
            (croissant, 'Croissant Tradicional', 2),
            (cookie, 'Cookie Calebaut', 2),
            (deco, 'Arranjo de Flor', 1),
        ])
        ped = _pedido(db, loja_retirada=loja, itens=[(cesta, 3)])  # 3 boxes

        r = loja_estoque_reserva.reservar(ped, loja_id=loja.id)
        assert r['ok'] is True
        assert r['reservas'] == 2                    # croissant + cookie; deco fora
        el_cro = EstoqueLoja.query.filter_by(
            loja_id=loja.id, receita_id=croissant.id).first()
        el_cok = EstoqueLoja.query.filter_by(
            loja_id=loja.id, receita_id=cookie.id).first()
        assert el_cro.quantidade_reservada == 6      # 3 boxes × 2
        assert el_cok.quantidade_reservada == 6
        # A cesta-produto NUNCA ganhou linha de estoque (nao foi tocada).
        assert EstoqueLoja.query.filter_by(
            loja_id=loja.id, produto_id=cesta.id).first() is None
        # Componente decorativo (sem estoque) nao virou linha fantasma.
        assert EstoqueLoja.query.filter_by(
            loja_id=loja.id, receita_id=deco.id).first() is None

        ped.reserva_expira_em = agora() + timedelta(minutes=30)
        db.session.commit()
        c = loja_estoque_reserva.consumir(ped, loja_id=loja.id)
        assert c['baixado'] == 12                    # 6 + 6
        db.session.refresh(el_cro)
        db.session.refresh(el_cok)
        assert el_cro.quantidade == 4                # 10 − 6
        assert el_cok.quantidade == 4
        assert el_cro.quantidade_reservada == 0
        movs = MovEstoqueLoja.query.filter_by(tipo='venda_site').all()
        assert len(movs) == 2 and {m.quantidade for m in movs} == {6}


def test_cesta_nao_bloqueia_por_componente_rastreado_sem_estoque(app):
    """DECISAO DO DONO (01/07/2026): componente de cesta e best-effort — a
    cesta VENDE mesmo com um componente rastreado sem saldo. Antes isso barrava
    o checkout inteiro (incidente: cliente nao conseguia comprar a 'Sweet
    Coffee' porque a base de MDF estava em 0). Item AVULSO segue protegido
    (test abaixo)."""
    from app.extensions import db
    from app.models import EstoqueLoja
    from app.services import loja_estoque_reserva
    with app.app_context():
        loja = _site_loja(db)
        croissant = _receita(db, 'Croissant Tradicional')
        cookie = _receita(db, 'Cookie Calebaut')
        _estoque_rec(db, loja, croissant, qtd=10)
        _estoque_rec(db, loja, cookie, qtd=1)        # so 1, cesta precisa de 2
        cesta = _cesta(db, 'Family Box', [
            (croissant, 'Croissant Tradicional', 2),
            (cookie, 'Cookie Calebaut', 2),
        ])
        ped = _pedido(db, loja_retirada=loja, itens=[(cesta, 1)])

        r = loja_estoque_reserva.reservar(ped, loja_id=loja.id)
        assert r['ok'] is True                       # NAO bloqueia mais
        assert r['sem_estoque'] == []
        assert r['reservas'] == 2
        # Reserva best-effort: cada componente reserva a demanda cheia (o cookie
        # fica com reservada > quantidade — disponivel clampa em 0, sem negativo).
        el_cro = EstoqueLoja.query.filter_by(
            loja_id=loja.id, receita_id=croissant.id).first()
        el_cok = EstoqueLoja.query.filter_by(
            loja_id=loja.id, receita_id=cookie.id).first()
        assert el_cro.quantidade_reservada == 2
        assert el_cok.quantidade_reservada == 2
        assert el_cok.disponivel == 0
        assert ped.reserva_expira_em is not None


def test_cesta_com_componente_zerado_vende_e_baixa_registra_falta(app):
    """Incidente 01/07/2026 (Sweet Coffee): a cesta tinha componente com linha
    de estoque em 0 (base de MDF). A reserva bloqueava o checkout. Agora vende;
    a baixa real registra venda_site_sem_estoque pro componente faltante e nao
    deixa o saldo negativo."""
    from app.extensions import db
    from app.models import EstoqueLoja, MovEstoqueLoja
    from app.services import loja_estoque_reserva
    from app.utils import agora
    with app.app_context():
        loja = _site_loja(db)
        suco = _receita(db, 'Suco de Uva Villa Piva 300ml')
        base = _receita(db, 'Base Quadrada MDF Pequena')
        _estoque_rec(db, loja, suco, qtd=5)
        _estoque_rec(db, loja, base, qtd=0)          # zerado, como no incidente
        cesta = _cesta(db, 'Sweet Coffee', [
            (suco, 'Suco de Uva Villa Piva 300ml', 1),
            (base, 'Base Quadrada MDF Pequena', 1),
        ])
        ped = _pedido(db, loja_retirada=loja, itens=[(cesta, 1)])

        r = loja_estoque_reserva.reservar(ped, loja_id=loja.id)
        assert r['ok'] is True                       # <- destravado

        ped.reserva_expira_em = agora() + timedelta(minutes=30)
        db.session.commit()
        loja_estoque_reserva.consumir(ped, loja_id=loja.id)

        el_suco = EstoqueLoja.query.filter_by(
            loja_id=loja.id, receita_id=suco.id).first()
        el_base = EstoqueLoja.query.filter_by(
            loja_id=loja.id, receita_id=base.id).first()
        assert el_suco.quantidade == 4               # 5 - 1 baixou normal
        assert el_base.quantidade == 0               # nao ficou negativo
        falta = MovEstoqueLoja.query.filter_by(
            estoque_loja_id=el_base.id,
            tipo='venda_site_sem_estoque').first()
        assert falta is not None                     # falta registrada, sem crash


def test_item_avulso_ainda_bloqueia_mesmo_dividindo_linha_com_cesta(app):
    """Anti-oversell preservado: quando o MESMO item e vendido AVULSO e tambem
    entra como componente de cesta no mesmo pedido, a parcela AVULSA ainda barra
    o checkout se nao ha saldo pra ela — so a parcela de cesta e best-effort."""
    from app.extensions import db
    from app.models import Produto, ProdutoItem
    from app.services import loja_estoque_reserva
    with app.app_context():
        loja = _site_loja(db)
        suco = _produto(db, 'Suco de Uva', preco=12)
        _estoque(db, loja, suco, qtd=1)              # so 1 no fisico
        cesta = Produto(nome='Cesta com Suco', categoria='Cestas',
                        preco_site=80.0, imagem_dropbox_url='https://x/c.jpg',
                        ativo=True)
        db.session.add(cesta)
        db.session.flush()
        db.session.add(ProdutoItem(produto_id=cesta.id, tipo='produto',
                                   produto_componente_id=suco.id,
                                   item_nome='Suco de Uva', quantidade=1))
        db.session.commit()
        # 2 sucos AVULSOS (precisa 2, so ha 1) + 1 cesta (componente pede +1).
        ped = _pedido(db, loja_retirada=loja, itens=[(suco, 2), (cesta, 1)])

        r = loja_estoque_reserva.reservar(ped, loja_id=loja.id)
        assert r['ok'] is False                      # a parcela avulsa nao cabe
        s = [x for x in r['sem_estoque'] if x['nome'] == 'Suco de Uva']
        assert s and s[0]['pedido'] == 2             # so a demanda AVULSA (nao 3)
        assert s[0]['disponivel'] == 1


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

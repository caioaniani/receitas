"""Vendas da loja propria (PedidoOnline) no faturamento (22/06/2026).

Antes: VNDA era a fonte das vendas online. VNDA desligado -> as vendas da
loja nativa tinham que entrar no faturamento/relatorios, senao a receita
online sumia. Cobre:
- so pedido PAGO e nao cancelado conta;
- faturamento = subtotal (EXCLUI frete);
- por data de venda (pago_em);
- per-produto pela FK do item;
- agregar_itens_consolidado soma o online no faturamento_total.
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


def _pedido(db, *, codigo, prod, qtd, subtotal, frete='0.00',
            status='pago', pago_em='auto'):
    from app.models import Cliente, PedidoOnline, PedidoOnlineItem
    from app.utils import agora
    cli = Cliente(nome='C', email=f'{codigo.lower()}@x.com')
    db.session.add(cli)
    db.session.flush()
    sub = Decimal(str(subtotal))
    p = PedidoOnline(
        codigo=codigo, cliente_id=cli.id, nome_cliente='C',
        email_cliente=cli.email, modo_entrega='retirada', status=status,
        subtotal=sub, frete_valor=Decimal(frete),
        valor_total=sub + Decimal(frete),
        pago_em=(agora() if pago_em == 'auto' else pago_em),
    )
    db.session.add(p)
    db.session.flush()
    p.itens.append(PedidoOnlineItem(
        kind='produto', produto_id=prod.id, nome=prod.nome,
        preco_unitario=(sub / qtd if qtd else Decimal('0')),
        quantidade=qtd, subtotal=sub))
    db.session.commit()
    return p


def test_faturamento_so_pago_sem_frete(app):
    from app.extensions import db
    from app.services import loja_online_vendas
    from app.utils import hoje
    with app.app_context():
        loja = _site_loja(db)
        prod = _produto(db, 'Pao Pago')
        _pedido(db, codigo='PG01', prod=prod, qtd=2, subtotal='100.00',
                frete='20.00', status='pago')
        _pedido(db, codigo='AG01', prod=prod, qtd=1, subtotal='50.00',
                status='aguardando_pagamento', pago_em=None)
        _pedido(db, codigo='CN01', prod=prod, qtd=1, subtotal='70.00',
                status='cancelado')
        h = hoje()
        fat = loja_online_vendas.faturamento_por_dia(h, h)
        # So o pago conta; frete (20) EXCLUIDO -> 100, nao 120.
        assert fat['total'] == 100.00
        assert fat['n_pedidos'] == 1


def test_vendas_por_produto_soma_qtd(app):
    from app.extensions import db
    from app.services import loja_online_vendas
    from app.utils import hoje
    with app.app_context():
        loja = _site_loja(db)
        a = _produto(db, 'Pao A')
        b = _produto(db, 'Pao B')
        _pedido(db, codigo='PA01', prod=a, qtd=3, subtotal='30.00')
        _pedido(db, codigo='PA02', prod=a, qtd=2, subtotal='20.00')
        _pedido(db, codigo='PB01', prod=b, qtd=5, subtotal='50.00')
        _pedido(db, codigo='CN02', prod=a, qtd=9, subtotal='90.00',
                status='cancelado')  # nao conta
        h = hoje()
        agg = loja_online_vendas.vendas_por_produto(h, h)
        assert agg[('produto', a.id)] == 5    # 3 + 2 (cancelado fora)
        assert agg[('produto', b.id)] == 5


def test_faturamento_respeita_intervalo_por_pago_em(app):
    from app.extensions import db
    from app.services import loja_online_vendas
    from app.utils import agora, hoje
    with app.app_context():
        loja = _site_loja(db)
        prod = _produto(db, 'Pao Data')
        ontem = agora() - timedelta(days=1)
        _pedido(db, codigo='HJ01', prod=prod, qtd=1, subtotal='40.00')
        _pedido(db, codigo='ON01', prod=prod, qtd=1, subtotal='99.00',
                pago_em=ontem)
        h = hoje()
        # So hoje -> 40; ontem fica de fora.
        assert loja_online_vendas.faturamento_por_dia(h, h)['total'] == 40.00
        # Janela cobrindo ontem+hoje -> 139.
        fat2 = loja_online_vendas.faturamento_por_dia(h - timedelta(days=1), h)
        assert fat2['total'] == 139.00


def test_consolidado_soma_online_no_total(app):
    from app.extensions import db
    from app.services import vendas_itens
    from app.utils import hoje
    with app.app_context():
        loja = _site_loja(db)
        prod = _produto(db, 'Pao Consolidado')
        _pedido(db, codigo='CS01', prod=prod, qtd=4, subtotal='80.00',
                frete='10.00')
        h = hoje()
        data = vendas_itens.agregar_itens_consolidado(h, h)
        # Sem Seru/VNDA no teste, o total = so o online (subtotal, sem frete).
        assert data['faturamento_online'] == 80.00
        assert data['faturamento_total'] == 80.00
        nomes = {p['nome']: p for p in data['produtos']}
        assert 'Pao Consolidado' in nomes
        assert nomes['Pao Consolidado']['qtd_online'] == 4

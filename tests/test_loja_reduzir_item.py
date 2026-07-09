"""Redução OWNER-ONLY de quantidade de item num pedido PAGO do site
(08/07/2026, decisão do dono — cliente comprou 2 e era 1).

Cobre o essencial (dinheiro + estoque peso especial):
- refund PARCIAL no Pagar.me = unidades removidas × preço (frete intacto);
- estoque volta só o delta (versão nova por baixo — motor único);
- cancelamento total DEPOIS da redução NÃO credita em dobro (o furo que a
  versão de referência resolve);
- guardas (só pago, mínimo 1, item inexistente, refund que falha aborta);
- plano-do-dia devolve o delta;
- rota owner-only + banner "corrigir NF no Tiny".
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


def _produto(db, nome='Bandeja', preco=435.0):
    from app.models import Produto
    p = Produto(nome=nome, categoria='Cestas', preco_site=preco,
                imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


def _estoque(db, loja, produto, qtd):
    from app.models import EstoqueLoja
    el = EstoqueLoja(loja_id=loja.id, produto_id=produto.id, quantidade=qtd)
    db.session.add(el)
    db.session.commit()
    return el


def _pedido_pago(db, loja, produto, qtd, *, codigo='AAAA0001', preco=435.0,
                 data_entrega=None, com_nf=False):
    """Cria pedido PAGO com estoque JÁ baixado (via consumir) e um pagamento
    pago com charge_id — o estado de partida da redução."""
    from app.models import Cliente, PagamentoOnline, PedidoOnline, PedidoOnlineItem
    from app.services import loja_estoque_reserva
    cli = Cliente(nome='Maria', email=f'{codigo.lower()}@x.com')
    db.session.add(cli)
    db.session.commit()
    p = PedidoOnline(
        codigo=codigo, cliente_id=cli.id, nome_cliente='Maria',
        email_cliente=cli.email, modo_entrega='retirada',
        loja_retirada_id=loja.id, status='pago',
        data_entrega=data_entrega,
        subtotal=Decimal('0'), frete_valor=Decimal('15'),
        valor_total=Decimal('0'),
        tiny_nota_fiscal_id=('909295776' if com_nf else None),
        nf_status=('autorizada' if com_nf else None),
    )
    db.session.add(p)
    db.session.flush()
    p.itens.append(PedidoOnlineItem(
        kind='produto', produto_id=produto.id, nome=produto.nome,
        preco_unitario=Decimal(str(preco)), quantidade=qtd,
        subtotal=Decimal(str(preco)) * qtd))
    p.recalcular_total()
    db.session.commit()
    # baixa o estoque + reserva o plano-do-dia como o webhook 'pago' faria
    loja_estoque_reserva.consumir(p, loja_id=loja.id)
    if data_entrega:
        from app.services import loja_pagamento
        loja_pagamento._reservar_no_plano_do_dia(p)
    db.session.add(PagamentoOnline(
        pedido_id=p.id, metodo='cartao', valor=p.valor_total, status='pago',
        pagarme_charge_id='ch_teste'))
    db.session.commit()
    return p


def _saldo(db, loja, produto):
    from app.models import EstoqueLoja
    el = EstoqueLoja.query.filter_by(loja_id=loja.id,
                                     produto_id=produto.id).first()
    return el.quantidade if el else None


# ── Redução feliz ──────────────────────────────────────────────────────

def test_reduz_2_para_1_estorna_delta_e_devolve_estoque(app):
    from app.extensions import db
    from app.services import loja_pagamento
    with app.app_context():
        loja = _site_loja(db)
        prod = _produto(db, preco=435.0)
        _estoque(db, loja, prod, 10)
        p = _pedido_pago(db, loja, prod, 2, preco=435.0)
        assert _saldo(db, loja, prod) == 8      # baixou 2 no pago

        with patch('app.services.pagarme.cancelar_charge',
                   return_value={'ok': True}) as m:
            item = p.itens[0]
            ok, msg = loja_pagamento.reduzir_item_pedido_pago(
                p, item.id, 1, usuario_id=None)
        assert ok, msg
        # refund PARCIAL = 1 × 435 (frete NÃO entra)
        m.assert_called_once()
        assert m.call_args.kwargs['valor_decimal'] == Decimal('435.00')
        # estoque: 8 -> +2 (estorno) -> -1 (rebaixa) = 9 (devolveu 1)
        assert _saldo(db, loja, prod) == 9
        db.session.refresh(p)
        assert p.itens[0].quantidade == 1
        assert p.subtotal == Decimal('435.00')
        assert p.valor_total == Decimal('450.00')     # 435 + 15 frete


def test_cancelar_total_apos_reducao_nao_credita_em_dobro(app):
    """O furo que a versão de referência fecha: reduzir 2->1 e DEPOIS cancelar
    tudo deve devolver no total 2 (não 3)."""
    from app.extensions import db
    from app.services import loja_pagamento
    with app.app_context():
        loja = _site_loja(db)
        prod = _produto(db, preco=435.0)
        _estoque(db, loja, prod, 10)
        p = _pedido_pago(db, loja, prod, 2)
        assert _saldo(db, loja, prod) == 8

        with patch('app.services.pagarme.cancelar_charge',
                   return_value={'ok': True}):
            loja_pagamento.reduzir_item_pedido_pago(p, p.itens[0].id, 1)
        assert _saldo(db, loja, prod) == 9            # devolveu 1

        # agora cancela tudo (reembolso total)
        with patch('app.services.pagarme.cancelar_charge',
                   return_value={'ok': True}):
            ok, _msg = loja_pagamento.reembolsar_pedido(p)
        assert ok
        # crédito do cancelamento = só o 1 que restou (não os 2 originais)
        assert _saldo(db, loja, prod) == 10           # total devolvido = 2
        db.session.refresh(p)
        assert p.status == 'cancelado'


def test_devolve_ao_plano_do_dia(app):
    from datetime import timedelta

    from app.extensions import db
    from app.models import EstoqueSitePlano
    from app.services import loja_pagamento
    from app.utils import hoje
    with app.app_context():
        loja = _site_loja(db)
        prod = _produto(db)
        _estoque(db, loja, prod, 10)
        d = hoje() + timedelta(days=1)
        p = _pedido_pago(db, loja, prod, 2, data_entrega=d)
        plano = EstoqueSitePlano.query.filter_by(
            kind='produto', item_id=prod.id, data=d).first()
        reservado_antes = plano.qtd_reservada if plano else 0

        with patch('app.services.pagarme.cancelar_charge',
                   return_value={'ok': True}):
            loja_pagamento.reduzir_item_pedido_pago(p, p.itens[0].id, 1)
        plano = EstoqueSitePlano.query.filter_by(
            kind='produto', item_id=prod.id, data=d).first()
        assert plano.qtd_reservada == reservado_antes - 1   # devolveu 1


def test_cesta_fracionaria_nao_deixa_fracao_fantasma(app):
    """Revisão 08/07: cesta com componente FRACIONÁRIO cria uma versão de
    baixa só-fracionária (sem mov inteiro). `_versao_estoque_atual` tem que
    detectá-la pelo DebitoEstoqueMov, senão o cancelamento total usa a versão
    errada e deixa fração pendente pra sempre."""
    from app.extensions import db
    from app.models import (
        DebitoEstoque,
        EstoqueLoja,
        PagamentoOnline,
        ProdutoItem,
        Receita,
    )
    from app.services import loja_estoque_reserva, loja_pagamento
    with app.app_context():
        loja = _site_loja(db)
        # receita componente + linha de estoque (pra baixa "ver" a linha)
        rec = Receita(nome='Recheio', categoria='Cremes', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100.0)
        cesta = _produto(db, nome='Kit Fracionado', preco=50.0)
        db.session.add(rec)
        db.session.flush()
        db.session.add(ProdutoItem(produto_id=cesta.id, tipo='receita',
                                   receita_id=rec.id, item_nome=rec.nome,
                                   quantidade=0.2))    # 0.2 por cesta
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=rec.id,
                                   quantidade=10))
        db.session.commit()

        # pedido pago de 2 cestas -> 2×0.2 = 0.4 fração (nenhum inteiro)
        from decimal import Decimal

        from app.models import Cliente, PedidoOnline, PedidoOnlineItem
        cli = Cliente(nome='F', email='frac@x.com')
        db.session.add(cli)
        db.session.commit()
        p = PedidoOnline(codigo='FRAC0001', cliente_id=cli.id, nome_cliente='F',
                         email_cliente=cli.email, modo_entrega='retirada',
                         loja_retirada_id=loja.id, status='pago',
                         subtotal=Decimal('0'), frete_valor=Decimal('0'),
                         valor_total=Decimal('0'))
        db.session.add(p)
        db.session.flush()
        p.itens.append(PedidoOnlineItem(
            kind='produto', produto_id=cesta.id, nome=cesta.nome,
            preco_unitario=Decimal('50'), quantidade=2, subtotal=Decimal('100')))
        p.recalcular_total()
        db.session.commit()
        loja_estoque_reserva.consumir(p, loja_id=loja.id)
        db.session.add(PagamentoOnline(pedido_id=p.id, metodo='cartao',
                                       valor=p.valor_total, status='pago',
                                       pagarme_charge_id='ch_frac'))
        db.session.commit()

        deb = DebitoEstoque.query.filter_by(loja_id=loja.id,
                                            receita_id=rec.id).first()
        assert abs((deb.fracao_pendente or 0) - 0.4) < 1e-6

        # reduz 2->1: sobra 0.2 pendente, sob a versão nova (só-fração)
        with patch('app.services.pagarme.cancelar_charge',
                   return_value={'ok': True}):
            loja_pagamento.reduzir_item_pedido_pago(p, p.itens[0].id, 1)
        db.session.refresh(deb)
        assert abs((deb.fracao_pendente or 0) - 0.2) < 1e-6
        assert loja_pagamento._versao_estoque_atual(p) == 1   # detecta a fração

        # cancela tudo: a versão atual (1) tem que reverter a fração -> ZERO
        with patch('app.services.pagarme.cancelar_charge',
                   return_value={'ok': True}):
            loja_pagamento.reembolsar_pedido(p)
        db.session.refresh(deb)
        assert abs(deb.fracao_pendente or 0) < 1e-6           # sem fantasma


# ── Guardas ────────────────────────────────────────────────────────────

def test_guarda_refund_falha_nao_mexe_em_nada(app):
    from app.extensions import db
    from app.services import loja_pagamento
    with app.app_context():
        loja = _site_loja(db)
        prod = _produto(db)
        _estoque(db, loja, prod, 10)
        p = _pedido_pago(db, loja, prod, 2)
        assert _saldo(db, loja, prod) == 8
        with patch('app.services.pagarme.cancelar_charge',
                   return_value={'ok': False, 'erro': 'gateway fora'}):
            ok, msg = loja_pagamento.reduzir_item_pedido_pago(
                p, p.itens[0].id, 1)
        assert ok is False and 'gateway fora' in msg
        assert _saldo(db, loja, prod) == 8            # estoque intacto
        db.session.refresh(p)
        assert p.itens[0].quantidade == 2             # nada mudou


def test_guarda_minimo_1_e_pedido_nao_pago(app):
    from app.extensions import db
    from app.services import loja_pagamento
    with app.app_context():
        loja = _site_loja(db)
        prod = _produto(db)
        _estoque(db, loja, prod, 10)
        p = _pedido_pago(db, loja, prod, 2)
        # nova >= atual, e nova < 1 -> recusa (sem chamar gateway)
        with patch('app.services.pagarme.cancelar_charge') as m:
            ok, _ = loja_pagamento.reduzir_item_pedido_pago(p, p.itens[0].id, 2)
            assert ok is False
            ok, _ = loja_pagamento.reduzir_item_pedido_pago(p, p.itens[0].id, 0)
            assert ok is False
            ok, _ = loja_pagamento.reduzir_item_pedido_pago(p, 999999, 1)
            assert ok is False           # item inexistente
            m.assert_not_called()
        # pedido não-pago recusa
        p.status = 'aguardando_pagamento'
        db.session.commit()
        ok, msg = loja_pagamento.reduzir_item_pedido_pago(p, p.itens[0].id, 1)
        assert ok is False and 'PAGO' in msg


# ── Rota + UI ──────────────────────────────────────────────────────────

def _login(app, *, owner):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='U', login='u-red',
                papel='admin' if owner else 'gerente', is_owner=owner)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def test_rota_reduzir_exige_owner(app):
    from app.extensions import db
    with app.app_context():
        loja = _site_loja(db)
        prod = _produto(db)
        _estoque(db, loja, prod, 10)
        p = _pedido_pago(db, loja, prod, 2)
        codigo = p.codigo
        item_id = p.itens[0].id

    c = _login(app, owner=False)              # gerente, não owner
    resp = c.post(f'/admin/loja-online/pedidos/{codigo}/reduzir-item',
                  data={'item_id': item_id, 'nova_qtd': 1})
    assert resp.status_code in (403, 302)
    with app.app_context():
        from app.models import PedidoOnline
        pp = PedidoOnline.query.filter_by(codigo=codigo).first()
        assert pp.itens[0].quantidade == 2        # não reduziu


def test_rota_reduzir_owner_funciona(app):
    from app.extensions import db
    with app.app_context():
        loja = _site_loja(db)
        prod = _produto(db, preco=435.0)
        _estoque(db, loja, prod, 10)
        p = _pedido_pago(db, loja, prod, 2, preco=435.0)
        codigo = p.codigo
        item_id = p.itens[0].id

    c = _login(app, owner=True)
    with patch('app.services.pagarme.cancelar_charge',
               return_value={'ok': True}):
        resp = c.post(f'/admin/loja-online/pedidos/{codigo}/reduzir-item',
                      data={'item_id': item_id, 'nova_qtd': 1},
                      follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        from app.models import PedidoOnline
        pp = PedidoOnline.query.filter_by(codigo=codigo).first()
        assert pp.itens[0].quantidade == 1


def test_banner_corrigir_nf_aparece_apos_reducao(app):
    from app.extensions import db
    from app.services import loja_pagamento
    with app.app_context():
        loja = _site_loja(db)
        prod = _produto(db)
        _estoque(db, loja, prod, 10)
        p = _pedido_pago(db, loja, prod, 2, com_nf=True)
        codigo = p.codigo
        with patch('app.services.pagarme.cancelar_charge',
                   return_value={'ok': True}):
            loja_pagamento.reduzir_item_pedido_pago(p, p.itens[0].id, 1)

    c = _login(app, owner=True)
    html = c.get(f'/admin/loja-online/pedidos/{codigo}').get_data(as_text=True)
    assert 'quantidade corrigida após a nf' in html.lower()
    assert '909295776' in html

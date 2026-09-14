"""Cada data do kit tem saída física própria e o ciclo expira por inteiro."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import event, text
from test_loja_estoque_reserva import _estoque, _pedido, _produto, _site_loja

from app.extensions import db
from app.models import (
    CompraKit,
    EntregaKit,
    EstoqueProducao,
    KitCafe,
    MovEstoqueLoja,
    MovEstoqueProducao,
    SaidaProducaoSite,
)
from app.services import kits_estoque, loja_estoque_reserva, saida_producao_site
from app.utils import agora, hoje


def _ciclo(owner, *, pago=True, saldo=20, datas=2):
    loja = _site_loja(db)
    produto = _produto(db, 'Pães do kit')
    estoque = _estoque(db, loja, produto, saldo, reservada=2)
    pedidos = []
    for ordem in range(datas):
        pedido = _pedido(db, codigo=f'KIT{ordem:05d}', loja_retirada=loja,
                         itens=[(produto, 3)], status='pago' if pago else 'aguardando_pagamento')
        pedido.data_entrega = hoje() + timedelta(days=ordem * 7)
        pedido.pago_em = agora() if pago else None
        pedidos.append(pedido)
    kit = KitCafe(nome='Café semanal', usuario_id=owner.id)
    db.session.add(kit)
    db.session.flush()
    compra = CompraKit(kit_id=kit.id, kit_nome=kit.nome, pedido_principal_id=pedidos[0].id,
                       subtotal=30 * datas, frete_total=0, valor_total=30 * datas,
                       checkout_token='kit-estoque', expira_em=agora() - timedelta(minutes=1),
                       pago_em=agora() if pago else None)
    db.session.add(compra)
    db.session.flush()
    for ordem, pedido in enumerate(pedidos, 1):
        db.session.add(EntregaKit(pedido_id=pedido.id, compra_id=compra.id, ordem=ordem))
    db.session.commit()
    return compra, pedidos, estoque, loja


def test_cada_dia_baixa_um_kit_e_repeticao_nao_desconta(app, admin_user):
    _, pedidos, estoque, _ = _ciclo(admin_user)
    assert estoque.quantidade == 20
    for pedido in pedidos:
        assert pedido.reserva_expira_em is None
    saida_producao_site.registrar(pedidos[0], 'coleta')
    db.session.commit()
    assert estoque.quantidade == 17
    assert estoque.quantidade_reservada == 2  # reserva de outro pedido intacta
    assert kits_estoque.ja_coletado(pedidos[0].id)
    assert not kits_estoque.ja_coletado(pedidos[1].id)
    saida_producao_site.registrar(pedidos[0], 'retry')
    saida_producao_site.registrar(pedidos[1], 'coleta_semana_seguinte')
    db.session.commit()
    assert estoque.quantidade == 14
    assert MovEstoqueLoja.query.filter_by(tipo='venda_site').count() == 2


def test_kit_misto_baixa_loja_e_industria_sem_duplicar(app, admin_user):
    _, pedidos, estoque, loja = _ciclo(admin_user, datas=1)
    pedido = pedidos[0]
    produto_sob = _produto(db, 'Croissant sob encomenda')
    produto_sob.sob_encomenda = True
    estoque_sob_loja = _estoque(db, loja, produto_sob, 40)
    industria = EstoqueProducao(produto_id=produto_sob.id, quantidade=50)
    db.session.add(industria)
    from app.models import PedidoOnlineItem
    pedido.itens.append(PedidoOnlineItem(kind='produto', produto_id=produto_sob.id,
                                        nome=produto_sob.nome, quantidade=2,
                                        preco_unitario=10, subtotal=20))
    db.session.commit()
    saida_producao_site.registrar(pedido, 'motorista')
    saida_producao_site.registrar(pedido, 'retry')
    db.session.commit()
    assert estoque.quantidade == 17
    assert estoque_sob_loja.quantidade == 40
    assert industria.quantidade == 48
    assert SaidaProducaoSite.query.count() == 1
    assert MovEstoqueProducao.query.filter_by(tipo='saida_site_direto').one().quantidade == 2
    assert kits_estoque.ja_coletado(pedido.id)


def test_falta_auditada_nao_baixa_reposicao_futura(app, admin_user):
    _, pedidos, estoque, _ = _ciclo(admin_user, saldo=0, datas=1)
    saida_producao_site.registrar(pedidos[0], 'coleta_sem_saldo')
    db.session.commit()
    assert estoque.quantidade == 0
    assert MovEstoqueLoja.query.filter_by(tipo='venda_site_sem_estoque').one().quantidade == 3
    estoque.quantidade = 30
    db.session.commit()
    saida_producao_site.registrar(pedidos[0], 'retry_apos_reposicao')
    db.session.commit()
    assert estoque.quantidade == 30
    assert MovEstoqueLoja.query.filter_by(tipo='venda_site_sem_estoque').count() == 1


def test_falha_no_marcador_desfaz_baixa_e_coleta(app, admin_user):
    _, pedidos, estoque, _ = _ciclo(admin_user, datas=1)

    def falhar(*_):
        raise RuntimeError('falha ao registrar coleta')

    event.listen(EntregaKit, 'before_update', falhar)
    try:
        with pytest.raises(RuntimeError, match='falha ao registrar coleta'):
            saida_producao_site.registrar(pedidos[0], 'coleta')
    finally:
        event.remove(EntregaKit, 'before_update', falhar)
        db.session.rollback()
    assert estoque.quantidade == 20
    assert not kits_estoque.ja_coletado(pedidos[0].id)
    assert MovEstoqueLoja.query.count() == 0


def test_acerto_manual_anterior_marca_coleta_sem_segunda_baixa(app, admin_user):
    from app.services import acerto_despacho
    _, pedidos, estoque, _ = _ciclo(admin_user, datas=1)
    industria = EstoqueProducao(produto_id=estoque.produto_id, quantidade=30)
    db.session.add(industria)
    db.session.commit()
    acerto_despacho.acertar(hoje(), executar=True)
    assert industria.quantidade == 27
    saida_producao_site.registrar(pedidos[0], 'coleta')
    db.session.commit()
    assert industria.quantidade == 27
    assert estoque.quantidade == 20
    assert MovEstoqueLoja.query.count() == 0
    assert kits_estoque.ja_coletado(pedidos[0].id)


def test_coleta_rele_itens_apos_trava(app, admin_user):
    _, pedidos, estoque, _ = _ciclo(admin_user, datas=1)
    item = pedidos[0].itens[0]
    db.session.execute(text('UPDATE pedido_online_item SET quantidade=1 WHERE id=:id'), {'id': item.id})
    assert item.quantidade == 3
    saida_producao_site.registrar(pedidos[0], 'coleta')
    db.session.commit()
    assert estoque.quantidade == 19


def test_coleta_rele_marcador_apos_trava(app, admin_user):
    _, pedidos, estoque, _ = _ciclo(admin_user, datas=1)
    entrega = db.session.get(EntregaKit, pedidos[0].id)
    db.session.execute(text('UPDATE entrega_kit SET coletado_em=:momento WHERE pedido_id=:id'),
                       {'id': pedidos[0].id, 'momento': agora()})
    assert entrega.coletado_em is None
    saida_producao_site.registrar(pedidos[0], 'retry')
    db.session.commit()
    assert estoque.quantidade == 20


def test_cron_expira_todas_as_datas_sem_tocar_estoque_ou_reserva(app, admin_user):
    _, pedidos, estoque, _ = _ciclo(admin_user, pago=False)
    with patch('app.services.loja_pagamento._devolver_ao_plano_do_dia') as devolver:
        assert loja_estoque_reserva.liberar_expirados() == [p.codigo for p in pedidos]
        assert loja_estoque_reserva.liberar_expirados() == []
    devolver.assert_not_called()
    assert all(p.status == 'cancelado' and p.motivo_cancelamento == 'pix_expirado' for p in pedidos)
    assert all(p.cancelado_em is not None for p in pedidos)
    assert estoque.quantidade == 20 and estoque.quantidade_reservada == 2
    assert MovEstoqueLoja.query.count() == 0


@pytest.mark.parametrize('mudanca', ['compra_paga', 'pedido_pago', 'prazo_prorrogado'])
def test_expiracao_revalida_pagamento_e_prazo_apos_esperar_trava(app, admin_user, mudanca):
    compra, pedidos, estoque, _ = _ciclo(admin_user, pago=False)
    refresh_original = db.session.refresh
    alterou = False

    def refresh_com_corrida(objeto, *args, **kwargs):
        nonlocal alterou
        if isinstance(objeto, CompraKit) and not alterou:
            alterou = True
            if mudanca == 'compra_paga':
                db.session.execute(text('UPDATE compra_kit SET pago_em=:hora WHERE id=:id'),
                                   {'id': compra.id, 'hora': agora()})
            elif mudanca == 'pedido_pago':
                db.session.execute(text("UPDATE pedido_online SET status='pago', pago_em=:hora WHERE id=:id"),
                                   {'id': pedidos[0].id, 'hora': agora()})
            else:
                db.session.execute(text('UPDATE compra_kit SET expira_em=:hora WHERE id=:id'),
                                   {'id': compra.id, 'hora': agora() + timedelta(minutes=30)})
        return refresh_original(objeto, *args, **kwargs)

    with patch.object(db.session, 'refresh', side_effect=refresh_com_corrida):
        assert kits_estoque.expirar_compras() == []
    assert alterou
    assert all(p.status != 'cancelado' for p in pedidos)
    assert estoque.quantidade == 20 and estoque.quantidade_reservada == 2


def test_nao_coleta_kit_sem_pagamento(app, admin_user):
    _, pedidos, estoque, _ = _ciclo(admin_user, pago=False)
    saida_producao_site.registrar(pedidos[0], 'tentativa')
    db.session.commit()
    assert estoque.quantidade == 20
    assert not kits_estoque.ja_coletado(pedidos[0].id)


def test_pedido_avulso_nao_baixa_novamente_na_coleta(app):
    loja = _site_loja(db)
    produto = _produto(db)
    estoque = _estoque(db, loja, produto, 10)
    pedido = _pedido(db, loja_retirada=loja, itens=[(produto, 2)], status='pago')
    saida_producao_site.registrar(pedido, 'legado')
    db.session.commit()
    assert estoque.quantidade == 10
    assert not kits_estoque.ja_coletado(pedido.id)
    assert MovEstoqueLoja.query.count() == 0


@pytest.mark.parametrize('status', ['solicitado', 'confirmado'])
def test_reembolso_em_andamento_ou_confirmado_bloqueia_coleta(app, admin_user, status):
    from app.models.kits_cafe import ReembolsoKit
    _, pedidos, estoque, _ = _ciclo(admin_user, datas=1)
    db.session.add(ReembolsoKit(pedido_id=pedidos[0].id, valor=30,
                               pagarme_charge_id='ch_kit', status=status))
    db.session.commit()
    with pytest.raises(ValueError, match='coleta deste kit está bloqueada'):
        saida_producao_site.registrar(pedidos[0], 'motorista')
    db.session.rollback()
    assert estoque.quantidade == 20
    assert not kits_estoque.ja_coletado(pedidos[0].id)


def test_reembolso_recusado_permite_coleta(app, admin_user):
    from app.models.kits_cafe import ReembolsoKit
    _, pedidos, estoque, _ = _ciclo(admin_user, datas=1)
    db.session.add(ReembolsoKit(pedido_id=pedidos[0].id, valor=30,
                               pagarme_charge_id='ch_kit', status='recusado'))
    db.session.commit()
    saida_producao_site.registrar(pedidos[0], 'motorista')
    db.session.commit()
    assert estoque.quantidade == 17
    assert kits_estoque.ja_coletado(pedidos[0].id)

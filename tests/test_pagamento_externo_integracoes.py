"""Recebimento externo frente a eventos tardios, expiração e estorno."""
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from test_loja_estoque_reserva import _estoque, _pedido, _produto, _site_loja

from app.extensions import db
from app.models import EstoqueSitePlano, PagamentoExternoOnline, PagamentoOnline, PedidoOnline, Usuario
from app.services import loja_estoque_reserva, loja_pagamento, pagamento_externo
from app.utils import agora, hoje


@pytest.fixture
def cenario(app, monkeypatch):
    for nome in ('_enviar_confirmacao', '_reportar_purchase'):
        monkeypatch.setattr(loja_pagamento, nome, Mock())
    owner = Usuario(nome='Dono', login='dono-ext-integracao', papel='admin',
                    is_owner=True, senha_hash='nao-usada')
    db.session.add(owner)
    loja = _site_loja(db)
    produto = _produto(db)
    saldo = _estoque(db, loja, produto, qtd=10)
    p = _pedido(db, loja_retirada=loja, itens=[(produto, 2)])
    p.data_entrega = hoje()
    pg = PagamentoOnline(pedido_id=p.id, metodo='pix', valor=p.valor_total,
                          pagarme_order_id='or_antiga', pagarme_charge_id='ch_antiga')
    db.session.add(pg)
    loja_estoque_reserva.reservar(p, loja_id=loja.id)
    db.session.commit()
    return p, pg, saldo, owner


def _confirmar(p, owner):
    ok, msg = pagamento_externo.confirmar_recebimento(
        p, usuario_id=owner.id, referencia='Pix identificado no extrato',
        valor_recebido='20,00', confirmado=True)
    assert ok, msg


@pytest.mark.parametrize('status', ['pago', 'em_preparo', 'a_caminho', 'entregue'])
@pytest.mark.parametrize('via', ['order.paid', 'charge.paid', 'conciliacao'])
def test_gateway_tardio_preserva_recebimento_e_entrega(cenario, status, via):
    p, pg, saldo, owner = cenario
    _confirmar(p, owner)
    confirmado_em = p.pago_em
    p.status = status
    db.session.commit()
    if via == 'conciliacao':
        with patch('app.services.pagarme.consultar_order', return_value={'ok': True, 'pago': True}):
            resposta = loja_pagamento.conciliar_pedido(p.codigo, aplicar=True)
        assert resposta['ok']
    else:
        identificador = 'ch_antiga' if via == 'charge.paid' else 'or_antiga'
        resposta = loja_pagamento.processar_webhook({
            'id': 'evt_tardio', 'type': via,
            'data': {'id': identificador, 'order': {'id': 'or_antiga', 'code': p.codigo}}})
        assert resposta['ok'] and not resposta['mudou']
    db.session.refresh(p)
    db.session.refresh(saldo)
    assert p.status == status and p.pago_em == confirmado_em
    assert saldo.quantidade == 8 and saldo.quantidade_reservada == 0
    assert EstoqueSitePlano.query.one().qtd_reservada == 2
    externo = db.session.get(PagamentoExternoOnline, p.id)
    assert db.session.get(PagamentoOnline, externo.pagamento_id).status == 'pago'
    assert pg.status == 'pago'  # registra também o dinheiro real recebido pelo QR antigo
    assert loja_pagamento._enviar_confirmacao.call_count == 1
    from app.models import TarefaFiscalPedido
    assert TarefaFiscalPedido.query.count() == 1
    assert loja_pagamento._reportar_purchase.call_count == 1


def test_charge_tardio_identifica_tentativa_correta_e_falha_nao_regride(cenario):
    p, pg, _, owner = cenario
    pg.status = 'falhou'
    outra = PagamentoOnline(pedido_id=p.id, metodo='cartao', valor=p.valor_total,
                             pagarme_order_id='or_outra', pagarme_charge_id='ch_outra')
    db.session.add(outra)
    db.session.commit()
    _confirmar(p, owner)
    for evento, tipo in [('evt_pago', 'charge.paid'), ('evt_falha', 'charge.payment_failed')]:
        res = loja_pagamento.processar_webhook({
            'id': evento, 'type': tipo, 'data': {'id': 'ch_antiga', 'code': p.codigo}})
        assert res['ok']
    assert pg.status == 'pago'
    assert outra.status == 'pendente'
    assert p.status == 'pago'


@pytest.mark.parametrize('com_qr', [True, False])
def test_estorno_e_reducao_externos_nao_chamam_gateway(cenario, com_qr):
    p, pg, saldo, owner = cenario
    if not com_qr:
        db.session.delete(pg)
        db.session.commit()
    _confirmar(p, owner)
    with patch('app.services.pagarme.cancelar_charge') as cancelar:
        assert not loja_pagamento.reembolsar_pedido(p)[0]
        assert not loja_pagamento.reduzir_item_pedido_pago(p, p.itens[0].id, 1, owner.id)[0]
    cancelar.assert_not_called()
    assert p.status == 'pago' and p.itens[0].quantidade == 2
    assert saldo.quantidade == 8


def test_expiracao_selecionada_antes_do_pagamento_rele_sob_lock(cenario, monkeypatch):
    p, _, saldo, owner = cenario
    p.reserva_expira_em = agora() - timedelta(minutes=1)
    db.session.commit()
    refresh_real = db.session.refresh
    confirmado = False

    def confirmar_antes_de_travar(obj, *args, **kwargs):
        nonlocal confirmado
        if isinstance(obj, PedidoOnline) and kwargs.get('with_for_update') and not confirmado:
            confirmado = True
            _confirmar(p, owner)
        return refresh_real(obj, *args, **kwargs)

    monkeypatch.setattr(db.session, 'refresh', confirmar_antes_de_travar)
    assert loja_estoque_reserva.liberar_expirados() == []
    assert confirmado and p.status == 'pago'
    assert saldo.quantidade == 8 and saldo.quantidade_reservada == 0


def test_reabrir_expirado_preserva_reserva_de_outro_cliente(cenario):
    p, _, saldo, owner = cenario
    outro = _pedido(db, codigo='OUTRO001', loja_retirada=p.loja_retirada,
                    itens=[(p.itens[0].produto, 3)])
    loja_estoque_reserva.reservar(outro, loja_id=saldo.loja_id)
    p.reserva_expira_em = agora() - timedelta(minutes=1)
    db.session.commit()
    assert loja_estoque_reserva.liberar_expirados() == [p.codigo]
    assert saldo.quantidade_reservada == 3
    _confirmar(p, owner)
    assert p.status == 'pago' and p.cancelado_em is None
    assert saldo.quantidade == 8 and saldo.quantidade_reservada == 3
    assert outro.status == 'aguardando_pagamento'


@pytest.mark.parametrize('metodo', ['pix', 'cartao'])
def test_checkout_obsoleto_nao_cobra_apos_confirmacao_externa(cenario, metodo):
    p, _, _, owner = cenario
    _confirmar(p, owner)
    # Simula objeto lido antes do POST do owner, sem gravar o estado obsoleto.
    from sqlalchemy.orm.attributes import set_committed_value
    set_committed_value(p, 'status', 'aguardando_pagamento')
    set_committed_value(p, 'pago_em', None)
    with patch(f'app.services.pagarme.criar_pedido_{metodo}') as cobrar:
        args = (p, 'token') if metodo == 'cartao' else (p,)
        pg, erros = getattr(loja_pagamento, f'iniciar_{metodo}')(*args)
    assert pg is None and erros
    cobrar.assert_not_called()
    assert p.status == 'pago'


@pytest.mark.parametrize('via', ['webhook', 'conciliacao'])
def test_gateway_commit_falha_nao_envia_confirmacao(cenario, via):
    p, _, saldo, _ = cenario
    commit_real = db.session.commit
    chamadas = 0

    def falhar_commit_pagamento():
        nonlocal chamadas
        chamadas += 1
        if via == 'conciliacao' or chamadas > 1:
            raise RuntimeError('Falha ao persistir pagamento')
        return commit_real()  # webhook persiste o evento antes da transação

    with patch.object(db.session, 'commit', side_effect=falhar_commit_pagamento):
        if via == 'conciliacao':
            with patch('app.services.pagarme.consultar_order', return_value={'ok': True, 'pago': True}):
                res = loja_pagamento.conciliar_pedido(p.codigo, aplicar=True)
        else:
            res = loja_pagamento.processar_webhook({
                'id': 'evt_rollback', 'type': 'order.paid', 'data': {'id': 'or_antiga'}})
    assert not res['ok']
    assert p.status == 'aguardando_pagamento' and p.pago_em is None
    assert saldo.quantidade == 10 and saldo.quantidade_reservada == 2
    assert EstoqueSitePlano.query.count() == 0
    loja_pagamento._enviar_confirmacao.assert_not_called()
    from app.models import TarefaFiscalPedido
    assert TarefaFiscalPedido.query.count() == 0

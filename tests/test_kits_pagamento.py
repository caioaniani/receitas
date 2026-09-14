"""Dinheiro do mês e efeitos individuais por data, sem chamadas externas reais."""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, call

import pytest
from sqlalchemy import text

from app.extensions import db
from app.models import (
    AppConfig,
    CompraKit,
    EntregaKit,
    EstoqueLoja,
    EstoqueSitePlano,
    KitCafe,
    MovEstoqueLoja,
    PagamentoExternoOnline,
    PagamentoOnline,
    PedidoOnline,
    PedidoOnlineItem,
    Receita,
    ReembolsoKit,
    TarefaFiscalKit,
)
from app.services import kits_pagamento, loja_pagamento, loja_plano_dia, pagamento_externo, pagarme
from app.utils import agora, hoje


@pytest.fixture(autouse=True)
def efeitos(monkeypatch):
    mocks = {}
    for nome in ('_enviar_confirmacao', '_emitir_nf_e_enviar', '_reportar_purchase'):
        mocks[nome] = Mock()
        monkeypatch.setattr(loja_pagamento, nome, mocks[nome])
    monkeypatch.setattr('app.services.email.disponivel', lambda: False)
    monkeypatch.setattr(pagarme.requests, 'post', Mock(side_effect=AssertionError('Rede não permitida')))
    monkeypatch.setattr(pagarme.requests, 'delete', Mock(side_effect=AssertionError('Rede não permitida')))
    monkeypatch.setattr(pagarme.requests, 'get', Mock(side_effect=AssertionError('Rede não permitida')))
    return mocks


@pytest.fixture
def compra(app, owner_user, loja):
    kit = KitCafe(nome='Café da manhã', usuario_id=owner_user.id)
    receita = Receita(nome='Croissant simples', categoria='Paes', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100)
    db.session.add_all([kit, receita])
    db.session.flush()
    AppConfig.set('loja_site_estoque_id', loja.id)
    db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=receita.id, quantidade=20))
    pedidos = []
    for indice, frete in enumerate(('10.00', '15.00'), 1):
        data = hoje() + timedelta(days=7 * indice)
        pedido = PedidoOnline(nome_cliente='Maria', email_cliente='maria@example.com',
                              modo_entrega='entrega', endereco_entrega='Rua do Café, 10',
                              data_entrega=data, janela_entrega='08h-09h',
                              subtotal=Decimal('40.00'), frete_valor=Decimal(frete),
                              valor_total=Decimal('40.00') + Decimal(frete))
        pedido.itens.append(PedidoOnlineItem(kind='receita', receita_id=receita.id,
                                             nome=receita.nome, quantidade=2,
                                             preco_unitario=Decimal('20.00'),
                                             subtotal=Decimal('40.00')))
        db.session.add(pedido)
        db.session.add(EstoqueSitePlano(kind='receita', item_id=receita.id, data=data,
                                       qtd_planejada=20, qtd_reservada=0))
        pedidos.append(pedido)
    db.session.flush()
    compra = CompraKit(kit_id=kit.id, kit_nome=kit.nome, pedido_principal_id=pedidos[0].id,
                       subtotal=Decimal('80.00'), frete_total=Decimal('25.00'),
                       valor_total=Decimal('105.00'), expira_em=agora() + timedelta(minutes=35),
                       checkout_token='a' * 64)
    db.session.add(compra)
    db.session.flush()
    for indice, pedido in enumerate(pedidos, 1):
        db.session.add(EntregaKit(pedido_id=pedido.id, compra_id=compra.id, ordem=indice))
    db.session.commit()
    return compra


def _pedidos(compra):
    return [registro.pedido for registro in compra.entregas]


def _tentativa(compra):
    pagamento = PagamentoOnline(pedido_id=compra.pedido_principal_id, metodo='pix',
                                valor=compra.valor_total, pagarme_order_id='or_mes',
                                pagarme_charge_id='ch_mes')
    db.session.add(pagamento)
    db.session.commit()
    return pagamento


def _evento(compra, evento='evt_pago', tipo='order.paid'):
    return {'id': evento, 'type': tipo,
            'data': {'id': 'or_mes', 'code': compra.pedido_principal.codigo}}


def _pagar(compra):
    pagamento = _tentativa(compra)
    resultado = loja_pagamento.processar_webhook(_evento(compra))
    assert resultado['ok'] and resultado['mudou']
    return pagamento


@pytest.mark.parametrize('metodo', ['pix', 'cartao'])
def test_gateway_recebe_uma_cobranca_com_produtos_e_frete_de_todas_datas(compra, metodo, monkeypatch):
    corpo = {'id': 'or_mes', 'charges': [{'id': 'ch_mes', 'status': 'pending',
                                        'last_transaction': {'qr_code': 'QR-MES'}}]}
    gateway = Mock(return_value=(200, corpo))
    monkeypatch.setattr(pagarme, '_post_order', gateway)
    secundario = _pedidos(compra)[1]
    if metodo == 'pix':
        pagamento, erros = loja_pagamento.iniciar_pix(secundario)
    else:
        pagamento, erros = loja_pagamento.iniciar_cartao(secundario, 'tok_teste')
    assert not erros
    assert pagamento.pedido_id == compra.pedido_principal_id
    assert pagamento.valor == Decimal('105.00')
    assert PagamentoOnline.query.count() == 1
    payload = gateway.call_args.args[0]
    assert payload['payments'][0]['amount'] == 10500
    assert sum(item['amount'] * item['quantity'] for item in payload['items']) == 10500
    assert len(payload['items']) == 4
    assert [item['amount'] for item in payload['items'] if item['code'].endswith('-frete')] == [1000, 1500]
    assert payload['code'] == compra.pedido_principal.codigo
    assert all(p.status == 'aguardando_pagamento' for p in _pedidos(compra))
    assert sum(p.valor_total for p in _pedidos(compra)) == compra.valor_total
    assert MovEstoqueLoja.query.count() == 0


@pytest.mark.parametrize('motivo', ['pago', 'cancelado', 'expirado'])
def test_impede_nova_cobranca_de_grupo_finalizado(compra, motivo, monkeypatch):
    if motivo == 'pago':
        _pagar(compra)
    elif motivo == 'cancelado':
        _pedidos(compra)[1].status = 'cancelado'
    else:
        compra.expira_em = agora() - timedelta(minutes=1)
    db.session.commit()
    gateway = Mock()
    monkeypatch.setattr(pagarme, 'criar_pedido_pix', gateway)
    pagamento, erros = loja_pagamento.iniciar_pix(compra.pedido_principal)
    assert pagamento is None and erros
    gateway.assert_not_called()


def test_pagamento_confirma_todas_datas_sem_debitar_fisico_e_emite_efeitos_corretos(compra, efeitos):
    pagamento = _pagar(compra)
    pedidos = _pedidos(compra)
    assert compra.pago_em
    assert all(p.status == 'pago' and p.pago_em == compra.pago_em for p in pedidos)
    assert pagamento.status == 'pago'
    assert [r.qtd_reservada for r in EstoqueSitePlano.query.order_by(EstoqueSitePlano.data)] == [2, 2]
    assert EstoqueLoja.query.one().quantidade == 20
    assert MovEstoqueLoja.query.count() == 0
    efeitos['_enviar_confirmacao'].assert_called_once_with(compra.pedido_principal)
    efeitos['_emitir_nf_e_enviar'].assert_not_called()
    assert {t.pedido_id for t in TarefaFiscalKit.query.all()} == {p.id for p in pedidos}
    assert efeitos['_reportar_purchase'].call_args_list == [call(p) for p in pedidos]


def test_webhook_tardio_nao_reabre_primeira_entregue_ou_segunda_cancelada(compra, efeitos):
    pagamento = _pagar(compra)
    primeiro, segundo = _pedidos(compra)
    primeiro.status = 'entregue'
    segundo.status = 'cancelado'
    segundo.motivo_cancelamento = 'reembolso'
    db.session.commit()
    for mock in efeitos.values():
        mock.reset_mock()
    resultado = loja_pagamento.processar_webhook(_evento(compra, evento='evt_pago_tardio'))
    assert resultado['ok'] and not resultado['mudou']
    assert primeiro.status == 'entregue' and segundo.status == 'cancelado'
    assert pagamento.status == 'pago'
    assert sum(r.qtd_reservada for r in EstoqueSitePlano.query.all()) == 4
    for mock in efeitos.values():
        mock.assert_not_called()


def test_erro_na_segunda_reserva_reverte_grupo_inteiro(compra, efeitos, monkeypatch):
    _tentativa(compra)
    original = loja_plano_dia.reservar
    chamadas = []

    def falhar_na_segunda(*args, **kwargs):
        chamadas.append(args)
        if len(chamadas) == 2:
            raise RuntimeError('Falha da segunda data')
        return original(*args, **kwargs)

    monkeypatch.setattr(loja_plano_dia, 'reservar', falhar_na_segunda)
    resultado = loja_pagamento.processar_webhook(_evento(compra))
    assert not resultado['ok']
    assert compra.pago_em is None
    assert all(p.status == 'aguardando_pagamento' and p.pago_em is None for p in _pedidos(compra))
    assert all(r.qtd_reservada == 0 for r in EstoqueSitePlano.query.all())
    assert PagamentoOnline.query.one().status == 'pendente'
    assert TarefaFiscalKit.query.count() == 0
    for mock in efeitos.values():
        mock.assert_not_called()


def test_recebimento_externo_pelo_child_exige_total_e_audita_somente_principal(compra, owner_user, efeitos):
    segundo = _pedidos(compra)[1]
    args = dict(usuario_id=owner_user.id, referencia='Pix do mês conferido', confirmado=True)
    assert not pagamento_externo.confirmar_recebimento(segundo, valor_recebido='55,00', **args)[0]
    assert pagamento_externo.confirmar_recebimento(segundo, valor_recebido='105,00', **args)[0]
    assert all(p.status == 'pago' for p in _pedidos(compra))
    registro = PagamentoExternoOnline.query.one()
    assert registro.pedido_id == compra.pedido_principal_id and registro.valor == Decimal('105.00')
    assert PagamentoOnline.query.one().valor == Decimal('105.00')
    assert loja_pagamento._tem_pagamento_externo(segundo)
    assert not loja_pagamento.reembolsar_pedido(segundo)[0]
    assert pagamento_externo.confirmar_recebimento(segundo, valor_recebido='105,00', **args)[0]
    efeitos['_enviar_confirmacao'].assert_called_once()
    assert MovEstoqueLoja.query.count() == 0


def test_reembolso_de_uma_data_preserva_cobranca_outro_pedido_e_estoque(compra, monkeypatch):
    pagamento = _pagar(compra)
    primeiro, segundo = _pedidos(compra)

    def devolver(charge, **args):
        assert charge == 'ch_mes' and args == {'valor_decimal': Decimal('50.00'), 'detalhar': True}
        with db.engine.connect() as conexao:
            assert conexao.execute(text('SELECT status FROM reembolso_kit')).scalar() == 'solicitado'
        return {'ok': True}

    gateway = Mock(side_effect=devolver)
    monkeypatch.setattr(pagarme, 'cancelar_charge', gateway)
    assert loja_pagamento.reembolsar_pedido(primeiro)[0]
    assert primeiro.status == 'cancelado' and segundo.status == 'pago'
    assert pagamento.status == 'pago' and compra.pago_em
    assert [r.qtd_reservada for r in EstoqueSitePlano.query.order_by(EstoqueSitePlano.data)] == [0, 2]
    assert EstoqueLoja.query.one().quantidade == 20
    assert MovEstoqueLoja.query.count() == 0
    assert ReembolsoKit.query.one().status == 'confirmado'
    assert loja_pagamento.reembolsar_pedido(primeiro)[0]
    gateway.assert_called_once()


def test_ultima_entrega_reembolsada_marca_cobranca_estornada(compra, monkeypatch):
    pagamento = _pagar(compra)
    gateway = Mock(return_value={'ok': True})
    monkeypatch.setattr(pagarme, 'cancelar_charge', gateway)
    for pedido in _pedidos(compra):
        assert loja_pagamento.reembolsar_pedido(pedido)[0]
    assert pagamento.status == 'estornado'
    assert gateway.call_args_list == [
        call('ch_mes', valor_decimal=Decimal('50.00'), detalhar=True),
        call('ch_mes', valor_decimal=Decimal('55.00'), detalhar=True)]
    assert sum(r.valor for r in ReembolsoKit.query.all()) == compra.valor_total
    resultado = loja_pagamento.processar_webhook(_evento(compra, evento='evt_tarde_estornado'))
    assert not resultado['mudou'] and pagamento.status == 'estornado'


@pytest.mark.parametrize('incerto', [False, True])
def test_recusa_ou_timeout_nao_cancela_entrega_nem_devolve_plano(compra, monkeypatch, incerto):
    pagamento = _pagar(compra)
    gateway = Mock(return_value={'ok': False, 'erro': 'Falha de teste', 'incerto': incerto})
    monkeypatch.setattr(pagarme, 'cancelar_charge', gateway)
    pedido = _pedidos(compra)[1]
    assert not loja_pagamento.reembolsar_pedido(pedido)[0]
    assert pedido.status == 'pago' and pagamento.status == 'pago'
    assert ReembolsoKit.query.one().status == ('solicitado' if incerto else 'recusado')
    assert sum(r.qtd_reservada for r in EstoqueSitePlano.query.all()) == 4
    assert not loja_pagamento.reembolsar_pedido(pedido)[0]
    assert gateway.call_count == (1 if incerto else 2)


def test_falha_local_apos_refund_retoma_sem_novo_dinheiro(compra, monkeypatch):
    _pagar(compra)
    gateway = Mock(return_value={'ok': True})
    monkeypatch.setattr(pagarme, 'cancelar_charge', gateway)
    original = kits_pagamento.cancelar_entrega_paga
    monkeypatch.setattr(kits_pagamento, 'cancelar_entrega_paga', Mock(side_effect=RuntimeError('Banco falhou')))
    pedido = _pedidos(compra)[1]
    assert not loja_pagamento.reembolsar_pedido(pedido)[0]
    assert pedido.status == 'pago'
    assert ReembolsoKit.query.one().status == 'confirmado'
    monkeypatch.setattr(kits_pagamento, 'cancelar_entrega_paga', original)
    assert loja_pagamento.reembolsar_pedido(pedido)[0]
    assert pedido.status == 'cancelado'
    gateway.assert_called_once()


def test_worker_que_retoma_refund_nao_devolve_plano_duas_vezes(compra, monkeypatch):
    _pagar(compra)
    pedido = _pedidos(compra)[1]
    plano = EstoqueSitePlano.query.filter_by(data=pedido.data_entrega).one()
    plano.qtd_reservada = 6  # 2 desta entrega, 4 de outros clientes
    db.session.commit()
    gateway = Mock(return_value={'ok': True})
    monkeypatch.setattr(pagarme, 'cancelar_charge', gateway)
    original = kits_pagamento.travar
    aquisicoes = []

    def concorrer(pedido, **kwargs):
        aquisicoes.append(pedido.id)
        if len(aquisicoes) == 3:
            # Simula outro worker concluindo entre o commit do comprovante e
            # a reaquisição de locks do primeiro; o estado deve ser relido.
            kits_pagamento.cancelar_entrega_paga(pedido)
            db.session.commit()
        return original(pedido, **kwargs)

    monkeypatch.setattr(kits_pagamento, 'travar', concorrer)
    assert loja_pagamento.reembolsar_pedido(pedido)[0]
    assert plano.qtd_reservada == 4
    gateway.assert_called_once()


def test_reducao_individual_bloqueada_antes_de_chamar_gateway(compra, monkeypatch):
    _pagar(compra)
    gateway = Mock()
    monkeypatch.setattr(pagarme, 'cancelar_charge', gateway)
    pedido = _pedidos(compra)[1]
    assert not loja_pagamento.reduzir_item_pedido_pago(pedido, pedido.itens[0].id, 1)[0]
    gateway.assert_not_called()
    assert pedido.itens[0].quantidade == 2


def test_email_unico_inclui_todas_datas_fretes_e_total_e_escapa_html(compra, monkeypatch):
    from app.services import email
    compra.kit_nome = '<script>alert(1)</script>'
    enviar = Mock(return_value={'ok': True})
    monkeypatch.setattr(email, 'enviar', enviar)
    assert email.enviar_confirmacao_pedido(_pedidos(compra)[1])['ok']
    enviar.assert_called_once()
    html = enviar.call_args.args[2]
    texto = enviar.call_args.kwargs['texto']
    assert '<script>' not in html and '&lt;script&gt;' in html
    for pedido in _pedidos(compra):
        assert pedido.data_entrega.strftime('%d/%m/%Y') in texto
        assert pedido.codigo in texto and pedido.janela_entrega in texto
    assert 'R$ 105,00' in texto and 'R$ 25,00' in texto
    assert 'sem renovação automática' in texto


def test_reembolso_apos_coleta_preserva_plano_e_nao_simula_devolucao_fisica(compra, monkeypatch):
    _pagar(compra)
    pedido = _pedidos(compra)[1]
    registro = db.session.get(EntregaKit, pedido.id)
    registro.coletado_em = agora()
    pedido.status = 'a_caminho'
    estoque = EstoqueLoja.query.one()
    estoque.quantidade = 18  # representa as duas unidades já retiradas na coleta
    db.session.commit()
    monkeypatch.setattr(pagarme, 'cancelar_charge', Mock(return_value={'ok': True}))
    assert loja_pagamento.reembolsar_pedido(pedido)[0]
    assert estoque.quantidade == 18
    assert sum(r.qtd_reservada for r in EstoqueSitePlano.query.all()) == 4
    assert MovEstoqueLoja.query.count() == 0


def test_conciliacao_por_codigo_do_child_confirma_grupo(compra, monkeypatch, efeitos):
    pagamento = _tentativa(compra)
    consulta = Mock(return_value={'ok': True, 'pago': True, 'status': 'paid'})
    monkeypatch.setattr(pagarme, 'consultar_order', consulta)
    resultado = loja_pagamento.conciliar_pedido(_pedidos(compra)[1].codigo, aplicar=True)
    assert resultado['ok'] and resultado['acao'] == 'MARCADO PAGO'
    consulta.assert_called_once_with('or_mes')
    assert compra.pago_em and pagamento.status == 'pago'
    assert all(p.status == 'pago' for p in _pedidos(compra))
    efeitos['_enviar_confirmacao'].assert_called_once_with(compra.pedido_principal)


def test_recebimento_externo_reabre_todas_datas_expiradas(compra, owner_user):
    for pedido in _pedidos(compra):
        pedido.status = 'cancelado'
        pedido.motivo_cancelamento = 'pix_expirado'
        pedido.cancelado_em = agora()
    compra.expira_em = agora() - timedelta(minutes=5)
    db.session.commit()
    pedido = _pedidos(compra)[1]
    assert pagamento_externo.pode_confirmar(pedido)
    assert pagamento_externo.confirmar_recebimento(
        pedido, usuario_id=owner_user.id, referencia='Pix recebido',
        valor_recebido='105,00', confirmado=True)[0]
    assert all(p.status == 'pago' and p.cancelado_em is None and p.motivo_cancelamento is None
               for p in _pedidos(compra))


@pytest.mark.parametrize(('http', 'corpo', 'ok', 'incerto'), [
    (200, {'id': 'ch_mes', 'status': 'paid',
           'last_transaction': {'status': 'partial_canceled', 'success': True}}, True, None),
    (200, {'id': 'ch_mes', 'status': 'canceled'}, True, None),
    (202, {'id': 'ch_mes', 'status': 'pending_refund'}, False, True),
    (200, {'id': 'ch_mes', 'status': 'pending_refund'}, False, True),
    (200, {'id': 'ch_mes', 'last_transaction': {'success': False}}, False, True),
    (200, {}, False, True),
    (200, {'id': 'ch_outra'}, False, True),
    (422, {'erro': 'Recusado'}, False, False),
    (500, {'erro': 'Falha temporária'}, False, True),
])
def test_resposta_detalhada_do_refund_nao_confunde_aceite_incerto_com_conclusao(
        app, monkeypatch, http, corpo, ok, incerto):
    app.config['PAGARME_API_KEY'] = 'sk_teste'
    resposta = Mock(status_code=http, text='resposta de teste')
    resposta.json.return_value = corpo
    delete = Mock(return_value=resposta)
    monkeypatch.setattr(pagarme.requests, 'delete', delete)
    resultado = pagarme.cancelar_charge('ch_mes', Decimal('55.00'), detalhar=True)
    assert resultado['ok'] is ok
    assert resultado.get('incerto') is incerto
    assert delete.call_args.kwargs['json'] == {'amount': 5500}


def test_capacidade_reservada_no_checkout_reutilizada_ao_pagar(compra):
    from app.services.kits_capacidade import reservar_compra
    assert reservar_compra(compra) == (True, [])
    db.session.commit()
    assert all(e.reserva_plano for e in compra.entregas)
    assert all(r.qtd_reservada == 2 for r in EstoqueSitePlano.query.all())
    assert EstoqueLoja.query.one().quantidade == 20
    _pagar(compra)
    assert all(r.qtd_reservada == 2 for r in EstoqueSitePlano.query.all())
    assert all(e.reserva_plano for e in compra.entregas)


@pytest.mark.parametrize('cancelamento', ['expiracao', 'owner'])
def test_compra_sem_pagamento_libera_capacidade_de_todas_datas_uma_vez(compra, cancelamento):
    from app.services import compra_kits, kits_estoque
    from app.services.kits_capacidade import reservar_compra
    assert reservar_compra(compra)[0]
    db.session.commit()
    if cancelamento == 'expiracao':
        compra.expira_em = agora() - timedelta(minutes=1)
        db.session.commit()
        assert len(kits_estoque.expirar_compras()) == 2
        db.session.commit()
        assert kits_estoque.expirar_compras() == []
    else:
        assert compra_kits.cancelar_pendentes(_pedidos(compra)[1])[0]
        assert compra_kits.cancelar_pendentes(compra.pedido_principal)[0]
    assert all(not e.reserva_plano for e in compra.entregas)
    assert all(r.qtd_reservada == 0 for r in EstoqueSitePlano.query.all())
    assert EstoqueLoja.query.one().quantidade == 20


def test_pagamento_apos_expiracao_registra_excesso_real_e_alerta_por_data(compra):
    from app.services import kits_estoque
    from app.services.kits_capacidade import reservar_compra
    assert reservar_compra(compra)[0]
    compra.expira_em = agora() - timedelta(minutes=1)
    db.session.commit()
    kits_estoque.expirar_compras()
    db.session.commit()
    primeiro = EstoqueSitePlano.query.order_by(EstoqueSitePlano.data).first()
    primeiro.qtd_reservada = primeiro.qtd_planejada  # capacidade já vendida a outros
    db.session.commit()
    _pagar(compra)
    assert primeiro.qtd_reservada == 22
    assert loja_plano_dia.saldo(primeiro.kind, primeiro.item_id, primeiro.data) == 0
    assert compra.entregas[0].alerta_capacidade
    assert 'excede o limite' in compra.entregas[0].alerta_capacidade
    assert compra.entregas[1].alerta_capacidade is None
    assert all(p.status == 'pago' for p in _pedidos(compra))
    assert all(e.reserva_plano for e in compra.entregas)
    assert MovEstoqueLoja.query.count() == 0


def test_data_sem_capacidade_reverte_reservas_anteriores_e_linha_nova(compra):
    from app.services.kits_capacidade import reservar_compra
    primeira, segunda = EstoqueSitePlano.query.order_by(EstoqueSitePlano.data).all()
    chave_primeira = (primeira.kind, primeira.item_id, primeira.data)
    db.session.delete(primeira)
    segunda.qtd_planejada = 0
    db.session.commit()
    ok, erros = reservar_compra(compra)
    assert not ok and erros
    db.session.rollback()
    assert EstoqueSitePlano.query.filter_by(kind=chave_primeira[0], item_id=chave_primeira[1],
                                            data=chave_primeira[2]).first() is None
    assert segunda.qtd_reservada == 0
    assert all(not e.reserva_plano for e in compra.entregas)


def test_expiracao_de_compras_sobrepostas_trava_planos_em_ordem_global(compra, monkeypatch):
    from app.services import kits_estoque
    from app.services.kits_capacidade import reservar_compra
    assert reservar_compra(compra)[0]
    dia_anterior = hoje() + timedelta(days=3)
    item_original = compra.pedido_principal.itens[0]
    pedido = PedidoOnline(nome_cliente='Outro cliente', email_cliente='outro@example.com',
                          modo_entrega='agendada', data_entrega=dia_anterior,
                          subtotal=Decimal('20'), frete_valor=Decimal('0'), valor_total=Decimal('20'))
    pedido.itens.append(PedidoOnlineItem(kind='receita', receita_id=item_original.receita_id,
                                         nome=item_original.nome, quantidade=1,
                                         preco_unitario=Decimal('20'), subtotal=Decimal('20')))
    db.session.add(pedido)
    db.session.flush()
    outra = CompraKit(kit_id=compra.kit_id, kit_nome=compra.kit_nome, pedido_principal_id=pedido.id,
                      subtotal=Decimal('20'), frete_total=Decimal('0'), valor_total=Decimal('20'),
                      expira_em=agora() - timedelta(minutes=1), checkout_token='b' * 64)
    db.session.add(outra)
    db.session.flush()
    db.session.add(EntregaKit(compra_id=outra.id, pedido_id=pedido.id, ordem=1))
    db.session.flush()
    assert reservar_compra(outra)[0]
    compra.expira_em = agora() - timedelta(minutes=1)
    db.session.commit()
    original = loja_plano_dia.devolver
    datas = []

    def registrar(kind, item_id, data, qtd, **kwargs):
        datas.append(data)
        return original(kind, item_id, data, qtd, **kwargs)

    monkeypatch.setattr(loja_plano_dia, 'devolver', registrar)
    assert len(kits_estoque.expirar_compras()) == 3
    assert datas == sorted(datas) and datas[0] == dia_anterior


def test_pago_rele_marcador_que_expiracao_alterou_enquanto_aguardava_lock(compra):
    from app.services.kits_capacidade import reservar_compra
    assert reservar_compra(compra)[0]
    db.session.commit()
    assert all(e.reserva_plano for e in compra.entregas)
    # SQL sem sincronizar identity map simula dados alterados por outro worker
    # após carregar o grupo, antes de obter suas travas.
    db.session.execute(text('UPDATE entrega_kit SET reserva_plano=0'))
    db.session.execute(text('UPDATE estoque_site_plano SET qtd_reservada=0'))
    db.session.execute(text("UPDATE pedido_online SET status='cancelado', motivo_cancelamento='pix_expirado'"))
    assert all(e.reserva_plano for e in compra.entregas)
    assert loja_pagamento._marcar_pago(compra.pedido_principal, None, enviar_confirmacao=False)
    assert all(r.qtd_reservada == 2 for r in EstoqueSitePlano.query.all())
    assert all(e.reserva_plano for e in compra.entregas)


def test_refund_rele_coleta_concorrente_e_preserva_capacidade(compra, monkeypatch):
    _pagar(compra)
    pedido = _pedidos(compra)[1]
    registro = db.session.get(EntregaKit, pedido.id)
    assert registro.coletado_em is None
    db.session.execute(text('UPDATE entrega_kit SET coletado_em=:instante WHERE pedido_id=:pid'),
                       {'instante': agora(), 'pid': pedido.id})
    assert registro.coletado_em is None
    monkeypatch.setattr(pagarme, 'cancelar_charge', Mock(return_value={'ok': True}))
    assert loja_pagamento.reembolsar_pedido(pedido)[0]
    assert all(r.qtd_reservada == 2 for r in EstoqueSitePlano.query.all())
    assert registro.coletado_em is not None


def test_cron_rele_reserva_concorrente_antes_de_liberar(compra):
    from app.services import kits_estoque
    assert all(not e.reserva_plano for e in compra.entregas)
    db.session.execute(text('UPDATE entrega_kit SET reserva_plano=1'))
    db.session.execute(text('UPDATE estoque_site_plano SET qtd_reservada=2'))
    db.session.execute(text('UPDATE compra_kit SET expira_em=:expirou'),
                       {'expirou': agora() - timedelta(minutes=1)})
    assert all(not e.reserva_plano for e in compra.entregas)
    assert len(kits_estoque.expirar_compras()) == 2
    assert all(r.qtd_reservada == 0 for r in EstoqueSitePlano.query.all())


def test_cron_confirma_lote_kit_antes_de_travar_avulso(compra, monkeypatch):
    from app.services import loja_estoque_reserva
    from app.services.kits_capacidade import reservar_compra
    assert reservar_compra(compra)[0]
    compra.expira_em = agora() - timedelta(minutes=1)
    avulso = PedidoOnline(nome_cliente='Avulso', email_cliente='avulso@example.com',
                          modo_entrega='agendada', reserva_expira_em=agora() - timedelta(minutes=1))
    db.session.add(avulso)
    db.session.commit()
    avulso_id = avulso.id
    kit_ids = [p.id for p in _pedidos(compra)]
    original = db.session.refresh
    conferiu = []

    def conferir_commit(objeto, *args, **kwargs):
        if isinstance(objeto, PedidoOnline) and objeto.id == avulso_id and kwargs.get('with_for_update'):
            with db.engine.connect() as conexao:
                assert conexao.execute(text('SELECT SUM(qtd_reservada) FROM estoque_site_plano')).scalar() == 0
                for pedido_id in kit_ids:
                    assert conexao.execute(text('SELECT status FROM pedido_online WHERE id=:pid'),
                                            {'pid': pedido_id}).scalar() == 'cancelado'
            conferiu.append(True)
        return original(objeto, *args, **kwargs)

    monkeypatch.setattr(db.session, 'refresh', conferir_commit)
    assert len(loja_estoque_reserva.liberar_expirados()) == 3
    assert conferiu == [True]

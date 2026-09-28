"""Auditoria do checkout do site (27-28/09/2026) — bloco dos kits de café.

1. O nonce do formulário só devolve a compra existente num duplo clique
   legítimo; formulário alterado ou compra encerrada pede um envio novo.
2. Estorno de entrega com resposta incerta é resolvido pela leitura da
   cobrança no Pagar.me, e uma parada bloqueada não derruba o início da rota.
3. Pagamento em dobro ou em compra cancelada avisa o dono; a expiração
   consulta o gateway (fora das travas) antes de cancelar.

Gateway, e-mail e WhatsApp sempre mockados — nenhuma rede real.
"""
import json
import logging
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests
from test_kits_checkout import BASE, TOKEN, _abrir, _agenda, _form, _post
from test_kits_checkout import frete as frete
from test_kits_checkout import kit as kit
from test_kits_estoque import _ciclo
from test_kits_pagamento import _evento, _pagar, _pedidos, _tentativa
from test_kits_pagamento import compra as compra

from app.extensions import db
from app.models import (
    AtribuicaoEntrega,
    CompraKit,
    Driver,
    EstoqueLoja,
    EstoqueSitePlano,
    KitCafeSuco,
    PagamentoOnline,
    PedidoOnline,
    Produto,
    ReembolsoKit,
    RotaInicio,
)
from app.services import (
    compra_kits,
    kits_estoque,
    loja_pagamento,
    pagamento_externo,
    pagarme,
    rastreio_entrega,
)
from app.utils import agora, hoje


@pytest.fixture
def checkout(app, monkeypatch):
    """Mesmo ambiente de test_kits_checkout (relógio do checkout fixo em BASE)."""
    from app.blueprints.loja import kits_routes
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    monkeypatch.setattr(compra_kits, 'agora', lambda: BASE)
    monkeypatch.setattr(kits_routes, 'agora', lambda: BASE)
    monkeypatch.setattr('app.services.email.disponivel', lambda: False)
    monkeypatch.setattr('app.services.loja_alerta.alertar_esgotado', Mock())
    monkeypatch.setattr('app.services.loja_alerta.alertar_endereco_falho', Mock())


@pytest.fixture
def gateway(app, monkeypatch):
    """Efeitos do pagamento mockados e rede proibida (padrão test_kits_pagamento)."""
    app.config['PAGARME_API_KEY'] = 'sk_test_auditoria'
    efeitos = SimpleNamespace(confirmacao=Mock(), purchase=Mock(), alerta=Mock())
    monkeypatch.setattr(loja_pagamento, '_enviar_confirmacao', efeitos.confirmacao)
    monkeypatch.setattr(loja_pagamento, '_reportar_purchase', efeitos.purchase)
    monkeypatch.setattr('app.services.email.disponivel', lambda: False)
    monkeypatch.setattr('app.services.loja_alerta.alertar_pedido_pago', efeitos.alerta)
    for metodo in ('post', 'delete', 'get'):
        monkeypatch.setattr(pagarme.requests, metodo,
                            Mock(side_effect=AssertionError('Rede não permitida')))
    return efeitos


# ── 1. Nonce do formulário ───────────────────────────────────────────────────

@pytest.fixture
def kit_escolhas(kit, loja):
    laranja = Produto(nome='Suco de laranja 1 L', ativo=True, site_ativo=True,
                      preco_site=Decimal('48.00'))
    verde = Produto(nome='Suco verde 1 L', ativo=True, site_ativo=True,
                    preco_site=Decimal('50.00'))
    cookie = Produto(nome='Cookie adicional', categoria='Cookies', ativo=True,
                     site_ativo=True, preco_site=Decimal('7.35'))
    db.session.add_all([laranja, verde, cookie])
    db.session.flush()
    for produto in (laranja, verde, cookie):
        db.session.add(EstoqueLoja(loja_id=loja.id, produto_id=produto.id,
                                   quantidade=40, quantidade_reservada=0))
    kit.sucos.extend([KitCafeSuco(produto_id=laranja.id), KitCafeSuco(produto_id=verde.id)])
    db.session.commit()
    return SimpleNamespace(kit=kit, laranja=laranja, verde=verde, cookie=cookie)


def _form_escolhas(cenario, **mudancas):
    dados = _form(suco_id=str(cenario.laranja.id), adicionais_json=json.dumps(
        [{'kind': 'produto', 'id': cenario.cookie.id, 'qtd': 2}]))
    dados.update(mudancas)
    return dados


def _criar(cenario, form=None, agenda=None, token=TOKEN):
    return compra_kits.criar_compra(
        cenario.kit, form or _form_escolhas(cenario),
        _agenda(1, 8) if agenda is None else agenda, checkout_token=token, base=BASE)


def _alteracao(cenario, nome):
    """(form, agenda) do segundo envio, mudando um único aspecto."""
    datas = [(BASE.date() + timedelta(days=dia)).isoformat() for dia in (1, 8)]
    if nome == 'outra data':
        return _form_escolhas(cenario), _agenda(1, 15)
    if nome == 'outro horário':
        return _form_escolhas(cenario), [{'data': datas[0], 'janela': '10:00–11:00'},
                                         {'data': datas[1], 'janela': '09:00–10:00'}]
    if nome == 'data a menos':
        return _form_escolhas(cenario), _agenda(1)
    if nome == 'outro suco':
        return _form_escolhas(cenario, suco_id=str(cenario.verde.id)), None
    if nome == 'adicional alterado':
        return _form_escolhas(cenario, adicionais_json=json.dumps(
            [{'kind': 'produto', 'id': cenario.cookie.id, 'qtd': 3}])), None
    if nome == 'sem adicional':
        form = _form_escolhas(cenario)
        del form['adicionais_json']
        return form, None
    campos = {'outro e-mail': {'email': 'outra@example.com'},
              'outro número': {'numero': '12'},
              'outro complemento': {'complemento': 'Apto 3'},
              'outro CEP': {'cep': '04077001'},
              'outro nome': {'nome': 'Catarina'}}
    return _form_escolhas(cenario, **campos[nome]), None


@pytest.mark.parametrize('alteracao', [
    'outra data', 'outro horário', 'data a menos', 'outro suco', 'adicional alterado',
    'sem adicional', 'outro e-mail', 'outro número', 'outro complemento', 'outro CEP',
    'outro nome'])
def test_formulario_alterado_com_mesmo_nonce_nao_devolve_compra_antiga(
        kit_escolhas, frete, checkout, alteracao):
    compra, erros = _criar(kit_escolhas)
    assert not erros and compra is not None
    frete.reset_mock()
    form, agenda = _alteracao(kit_escolhas, alteracao)
    with pytest.raises(compra_kits.FormularioJaUsado) as exc:
        _criar(kit_escolhas, form, agenda)
    assert str(exc.value) == (
        f'Este formulário já gerou a compra {compra.pedido_principal.codigo}. '
        'Confira os dados e envie de novo para criar uma nova compra.')
    # Nada é cotado nem gravado; a compra anterior fica como estava.
    frete.assert_not_called()
    assert CompraKit.query.count() == 1 and PedidoOnline.query.count() == 2
    assert all(e.pedido.status == 'aguardando_pagamento' for e in compra.entregas)


def test_duplo_clique_com_mesmo_conteudo_devolve_a_mesma_compra(kit_escolhas, frete, checkout):
    compra, _ = _criar(kit_escolhas)
    # Mesma escolha, com diferenças só de digitação/ordem que o checkout normaliza.
    form = _form_escolhas(kit_escolhas, email='  MARIA@Example.com ', cep='04077-000',
                          logradouro='Rua  das Flores ')
    mesma, erros = _criar(kit_escolhas, form, list(reversed(_agenda(1, 8))))
    assert not erros and mesma.id == compra.id
    assert CompraKit.query.count() == 1
    frete.assert_called_once()


@pytest.mark.parametrize('situacao', ['expirada', 'paga', 'cancelada'])
def test_mesmo_conteudo_com_compra_encerrada_exige_novo_envio(
        kit_escolhas, frete, checkout, situacao):
    compra, _ = _criar(kit_escolhas)
    if situacao == 'expirada':
        compra.expira_em = BASE - timedelta(minutes=1)
    elif situacao == 'paga':
        compra.pago_em = BASE
        for entrega in compra.entregas:
            entrega.pedido.status, entrega.pedido.pago_em = 'pago', BASE
    else:
        assert compra_kits.cancelar_pendentes(compra.pedido_principal)[0]
    db.session.commit()
    with pytest.raises(compra_kits.FormularioJaUsado):
        _criar(kit_escolhas)
    assert CompraKit.query.count() == 1


@pytest.mark.loja_host
def test_rota_troca_o_nonce_e_reapresenta_os_dados_enviados(app, kit, frete, checkout):
    cliente, token, _ = _abrir(app, kit)
    primeira = _post(cliente, kit, token)
    assert primeira.status_code == 302
    compra = CompraKit.query.one()
    # Volta pelo histórico, muda data e nome e envia o formulário antigo.
    resposta = _post(cliente, kit, token, agenda=_agenda(1, 15), nome='Catarina')
    assert resposta.status_code == 409
    html = resposta.get_data(as_text=True)
    assert (f'Este formulário já gerou a compra {compra.pedido_principal.codigo}. '
            'Confira os dados e envie de novo para criar uma nova compra.') in html
    assert 'value="Catarina"' in html
    assert (BASE.date() + timedelta(days=15)).isoformat() in html
    with cliente.session_transaction() as sessao:
        novo = sessao['kits_checkout_tokens'][str(kit.id)]
    assert novo != token and f'value="{novo}"' in html
    assert CompraKit.query.count() == 1
    # Reenvio consciente, com o nonce novo, cria a compra nova.
    nova = _post(cliente, kit, novo, agenda=_agenda(1, 15), nome='Catarina')
    assert nova.status_code == 302 and nova.location != primeira.location
    assert CompraKit.query.count() == 2


# ── 2. Estorno incerto e início da rota ──────────────────────────────────────

def _estorno_incerto(pedido, monkeypatch):
    delete = Mock(return_value={'ok': False, 'incerto': True,
                                'erro': 'O gateway ainda não confirmou a conclusão do estorno.'})
    monkeypatch.setattr(pagarme, 'cancelar_charge', delete)
    assert not loja_pagamento.reembolsar_pedido(pedido)[0]
    assert db.session.get(ReembolsoKit, pedido.id).status == 'solicitado'
    return delete


def _cobranca(monkeypatch, corpo=None, *, http=200, erro=None, sem_json=False):
    resposta = Mock(status_code=http, text='resposta de teste')
    if sem_json:
        resposta.json.side_effect = ValueError('sem JSON')
    else:
        resposta.json.return_value = corpo
    leitura = Mock(side_effect=erro) if erro else Mock(return_value=resposta)
    monkeypatch.setattr(pagarme.requests, 'get', leitura)
    return leitura


def _reservas():
    return [r.qtd_reservada for r in EstoqueSitePlano.query.order_by(EstoqueSitePlano.data)]


def test_estorno_parcial_confirmado_na_cobranca_cancela_so_esta_entrega(compra, gateway, monkeypatch):
    pagamento = _pagar(compra)
    primeiro, segundo = _pedidos(compra)
    delete = _estorno_incerto(segundo, monkeypatch)
    # Refund PARCIAL mantém a cobrança 'paid'; o valor devolvido é o que prova.
    leitura = _cobranca(monkeypatch, {
        'id': 'ch_mes', 'status': 'paid', 'amount': 10500, 'paid_amount': 10500,
        'canceled_amount': 5500, 'last_transaction': {'status': 'captured', 'success': True}})
    ok, mensagem = loja_pagamento.reembolsar_pedido(segundo)
    assert ok, mensagem
    assert leitura.call_args.args[0].endswith('/charges/ch_mes')
    delete.assert_called_once()   # o estorno nunca é reenviado
    registro = db.session.get(ReembolsoKit, segundo.id)
    assert registro.status == 'confirmado' and registro.confirmado_em is not None
    assert segundo.status == 'cancelado' and segundo.motivo_cancelamento == 'reembolso'
    assert primeiro.status == 'pago' and pagamento.status == 'pago'
    assert _reservas() == [2, 0]


@pytest.mark.parametrize('leitura', [
    {'corpo': {'id': 'ch_mes', 'status': 'paid', 'canceled_amount': 0,
               'last_transaction': {'status': 'pending_refund'}}},
    {'corpo': {'id': 'ch_mes', 'status': 'paid', 'canceled_amount': 5000}},
    {'corpo': {'id': 'ch_mes', 'status': 'paid'}},
    {'corpo': {'id': 'ch_mes', 'status': 'paid', 'canceled_amount': '5500'}},
    {'corpo': {'id': 'ch_outra', 'status': 'refunded', 'canceled_amount': 10500}},
    {'corpo': {'id': 'ch_mes', 'status': 'paid',
               'last_transaction': {'status': 'partial_refunded', 'amount': 10500}}},
    {'http': 500, 'corpo': {'erro': 'Falha temporária'}},
    {'erro': requests.Timeout('timeout')},
    {'sem_json': True},
], ids=['pendente', 'valor-menor', 'sem-valor', 'valor-texto', 'outra-cobranca',
        'parcial-ambiguo', 'http-500', 'timeout', 'sem-json'])
def test_estorno_incerto_sem_prova_segue_solicitado(compra, gateway, monkeypatch, leitura):
    _pagar(compra)
    segundo = _pedidos(compra)[1]
    delete = _estorno_incerto(segundo, monkeypatch)
    _cobranca(monkeypatch, **leitura)
    ok, mensagem = loja_pagamento.reembolsar_pedido(segundo)
    assert not ok and 'sem conclusão confirmada' in mensagem
    registro = db.session.get(ReembolsoKit, segundo.id)
    assert registro.status == 'solicitado' and registro.erro
    assert segundo.status == 'pago' and _reservas() == [2, 2]
    delete.assert_called_once()


def test_recusa_explicita_do_gateway_libera_nova_solicitacao(compra, gateway, monkeypatch):
    _pagar(compra)
    segundo = _pedidos(compra)[1]
    delete = _estorno_incerto(segundo, monkeypatch)
    _cobranca(monkeypatch, {'id': 'ch_mes', 'status': 'paid', 'canceled_amount': 0,
                            'last_transaction': {'status': 'error_on_refunding',
                                                 'success': False}})
    ok, mensagem = loja_pagamento.reembolsar_pedido(segundo)
    assert not ok and 'recusou o estorno anterior' in mensagem
    assert db.session.get(ReembolsoKit, segundo.id).status == 'recusado'
    assert segundo.status == 'pago' and _reservas() == [2, 2]
    delete.assert_called_once()   # a recusa não reenvia sozinha
    delete.return_value = {'ok': True}
    ok, mensagem = loja_pagamento.reembolsar_pedido(segundo)
    assert ok, mensagem
    assert delete.call_count == 2 and segundo.status == 'cancelado'


def test_duas_entregas_incertas_so_confirmam_quando_a_cobranca_cobre_as_duas(
        compra, gateway, monkeypatch):
    pagamento = _pagar(compra)
    primeiro, segundo = _pedidos(compra)
    _estorno_incerto(segundo, monkeypatch)
    _estorno_incerto(primeiro, monkeypatch)
    # Devolveu 55,00: poderia ser qualquer uma das duas — nenhuma é confirmada.
    _cobranca(monkeypatch, {'id': 'ch_mes', 'status': 'paid', 'canceled_amount': 5500})
    assert not loja_pagamento.reembolsar_pedido(segundo)[0]
    assert not loja_pagamento.reembolsar_pedido(primeiro)[0]
    assert {r.status for r in ReembolsoKit.query.all()} == {'solicitado'}
    _cobranca(monkeypatch, {'id': 'ch_mes', 'status': 'refunded', 'canceled_amount': 10500})
    assert loja_pagamento.reembolsar_pedido(segundo)[0]
    assert loja_pagamento.reembolsar_pedido(primeiro)[0]
    assert primeiro.status == segundo.status == 'cancelado'
    assert pagamento.status == 'estornado' and _reservas() == [0, 0]


def test_cobranca_inteira_estornada_sem_canceled_amount_confirma(compra, gateway, monkeypatch):
    _pagar(compra)
    segundo = _pedidos(compra)[1]
    _estorno_incerto(segundo, monkeypatch)
    _cobranca(monkeypatch, {'id': 'ch_mes', 'status': 'refunded', 'paid_amount': 10500})
    assert loja_pagamento.reembolsar_pedido(segundo)[0]
    assert segundo.status == 'cancelado'


def test_parada_bloqueada_nao_derruba_o_inicio_da_rota(app, admin_user, caplog):
    _, pedidos, estoque, _ = _ciclo(admin_user, datas=2)
    for pedido in pedidos:
        pedido.data_entrega = hoje()
    db.session.add(ReembolsoKit(pedido_id=pedidos[0].id, valor=30,
                               pagarme_charge_id='ch_kit', status='solicitado'))
    motorista = Driver(nome='Motorista dos kits', ativo=True, token='kits-rota', pin='1234')
    db.session.add(motorista)
    db.session.flush()
    for ordem, pedido in enumerate(pedidos, 1):
        db.session.add(AtribuicaoEntrega(driver_id=motorista.id, pedido_code=pedido.codigo,
                                        data_entrega=hoje(), ordem=ordem))
    db.session.commit()
    caplog.set_level(logging.ERROR, logger='app.services.rastreio_entrega')
    with patch('app.services.rastreio_entrega._enviar_emails_saida', return_value=0):
        marco, _ = rastreio_entrega.iniciar_rota(motorista, hoje())
    assert marco.id and RotaInicio.query.count() == 1
    # A parada com estorno pendente fica sem coleta; a outra sai normalmente.
    assert not kits_estoque.ja_coletado(pedidos[0].id)
    assert kits_estoque.ja_coletado(pedidos[1].id)
    assert estoque.quantidade == 17
    assert any(pedidos[0].codigo in r.getMessage() and 'bloqueada' in r.getMessage()
               for r in caplog.records)


# ── 3. Pagamento em dobro, compra cancelada e expiração ──────────────────────

def test_segundo_pagamento_na_mesma_compra_avisa_o_dono(compra, gateway):
    _pagar(compra)
    segunda = PagamentoOnline(pedido_id=compra.pedido_principal_id, metodo='cartao',
                              valor=compra.valor_total, pagarme_order_id='or_segunda',
                              pagarme_charge_id='ch_segunda')
    db.session.add(segunda)
    db.session.commit()
    # Reentrega (charge.paid) da tentativa que já pagou: não é duplicidade.
    repetido = loja_pagamento.processar_webhook(
        {'id': 'evt_repetido', 'type': 'charge.paid', 'data': {'id': 'ch_mes'}})
    assert repetido['ok'] and not repetido['mudou']
    gateway.alerta.assert_not_called()
    resultado = loja_pagamento.processar_webhook(
        {'id': 'evt_segunda', 'type': 'order.paid', 'data': {'id': 'or_segunda'}})
    assert resultado['ok'] and not resultado['mudou']
    assert segunda.status == 'pago'
    gateway.alerta.assert_called_once()
    texto, chave = gateway.alerta.call_args.args
    assert 'PAGA DUAS VEZES' in texto and compra.pedido_principal.codigo in texto
    assert 'R$ 105,00' in texto
    assert chave == f'kit_duplicado|{compra.id}|{segunda.id}'
    loja_pagamento.processar_webhook(
        {'id': 'evt_segunda_charge', 'type': 'charge.paid', 'data': {'id': 'ch_segunda'}})
    gateway.alerta.assert_called_once()
    gateway.confirmacao.assert_called_once()


def test_gateway_paga_depois_do_recebimento_externo_avisa_o_dono(compra, gateway, owner_user):
    tentativa = _tentativa(compra)
    assert pagamento_externo.confirmar_recebimento(
        compra.pedido_principal, usuario_id=owner_user.id, referencia='Pix na conta',
        valor_recebido='105,00', confirmado=True)[0]
    gateway.alerta.assert_not_called()
    resultado = loja_pagamento.processar_webhook(_evento(compra, evento='evt_depois_externo'))
    assert resultado['ok'] and not resultado['mudou']
    gateway.alerta.assert_called_once()
    texto, chave = gateway.alerta.call_args.args
    assert 'PAGA DUAS VEZES' in texto
    assert chave == f'kit_duplicado|{compra.id}|{tentativa.id}'


def test_pagamento_em_compra_cancelada_de_proposito_avisa_e_mantem_cancelada(compra, gateway):
    tentativa = _tentativa(compra)
    assert compra_kits.cancelar_pendentes(compra.pedido_principal)[0]
    resultado = loja_pagamento.processar_webhook(_evento(compra, evento='evt_pago_cancelada'))
    assert resultado['ok'] and not resultado['mudou']
    assert all(p.status == 'cancelado' and p.motivo_cancelamento == 'cancelado_admin'
               for p in _pedidos(compra))
    assert compra.pago_em is None and tentativa.status == 'pago'
    gateway.alerta.assert_called_once()
    texto, chave = gateway.alerta.call_args.args
    assert 'CANCELADA e recebeu pagamento' in texto and compra.pedido_principal.codigo in texto
    assert 'R$ 105,00' in texto
    assert chave == f'kit_cancelado_pago|{compra.id}|{tentativa.id}'
    # charge.paid da mesma tentativa chega em seguida: o aviso não se repete.
    loja_pagamento.processar_webhook(
        {'id': 'evt_charge_cancelada', 'type': 'charge.paid', 'data': {'id': 'ch_mes'}})
    gateway.alerta.assert_called_once()
    assert all(p.status == 'cancelado' for p in _pedidos(compra))
    gateway.confirmacao.assert_not_called()


def _vencida(compra, *, pix_expira_em):
    from app.services.kits_capacidade import reservar_compra
    assert reservar_compra(compra)[0]
    tentativa = _tentativa(compra)
    tentativa.pix_expira_em = pix_expira_em
    compra.expira_em = agora() - timedelta(minutes=1)
    db.session.commit()
    return tentativa


def test_expiracao_com_qr_do_pix_valido_adia_sem_cancelar(compra, gateway, monkeypatch):
    _vencida(compra, pix_expira_em=agora() + timedelta(minutes=10))
    monkeypatch.setattr(pagarme, 'consultar_order', Mock(side_effect=AssertionError(
        'QR válido: não precisa consultar')))
    base = agora()
    assert kits_estoque.expirar_compras(base=base) == []
    assert compra.expira_em == base + timedelta(minutes=5)
    assert all(p.status == 'aguardando_pagamento' for p in _pedidos(compra))
    assert all(e.reserva_plano for e in compra.entregas) and _reservas() == [2, 2]
    gateway.alerta.assert_not_called()


def test_expiracao_confirma_o_que_o_gateway_ja_recebeu_consultando_sem_trava(
        compra, gateway, monkeypatch):
    tentativa = _vencida(compra, pix_expira_em=agora() - timedelta(minutes=1))
    eventos = []

    def consultar(order_id):
        assert order_id == 'or_mes'
        eventos.append('consulta')
        return {'ok': True, 'pago': True, 'status': 'paid', 'charge_status': 'paid'}

    monkeypatch.setattr(pagarme, 'consultar_order', consultar)
    refresh = db.session.refresh

    def registrar_trava(objeto, *args, **kwargs):
        if isinstance(objeto, CompraKit) and kwargs.get('with_for_update'):
            eventos.append('trava')
        return refresh(objeto, *args, **kwargs)

    monkeypatch.setattr(db.session, 'refresh', registrar_trava)
    assert kits_estoque.expirar_compras() == []
    assert eventos[0] == 'consulta' and 'trava' in eventos
    assert compra.pago_em is not None and tentativa.status == 'pago'
    assert all(p.status == 'pago' for p in _pedidos(compra))
    assert _reservas() == [2, 2]
    gateway.confirmacao.assert_called_once_with(compra.pedido_principal)


def test_expiracao_com_gateway_fora_adia(compra, gateway, monkeypatch):
    _vencida(compra, pix_expira_em=agora() - timedelta(minutes=1))
    monkeypatch.setattr(pagarme, 'consultar_order',
                        Mock(return_value={'ok': False, 'erro': 'timeout'}))
    base = agora()
    assert kits_estoque.expirar_compras(base=base) == []
    assert compra.expira_em == base + timedelta(minutes=5)
    assert all(p.status == 'aguardando_pagamento' for p in _pedidos(compra))


def test_expiracao_sem_nada_vivo_no_gateway_cancela(compra, gateway, monkeypatch):
    tentativa = _vencida(compra, pix_expira_em=agora() - timedelta(minutes=1))
    monkeypatch.setattr(pagarme, 'consultar_order', Mock(return_value={
        'ok': True, 'pago': False, 'status': 'failed', 'charge_status': 'failed'}))
    codigos = kits_estoque.expirar_compras()
    assert codigos == [p.codigo for p in _pedidos(compra)]
    assert tentativa.status == 'falhou'
    assert all(p.status == 'cancelado' and p.motivo_cancelamento == 'pix_expirado'
               for p in _pedidos(compra))
    assert _reservas() == [0, 0]


def test_compra_presa_no_gateway_ha_mais_de_24h_avisa_o_dono(compra, gateway, monkeypatch):
    _vencida(compra, pix_expira_em=agora() + timedelta(minutes=10))
    compra.criado_em = agora() - timedelta(hours=25)
    db.session.commit()
    assert kits_estoque.expirar_compras() == []
    gateway.alerta.assert_called_once()
    texto, chave = gateway.alerta.call_args.args
    assert 'mais de 24 h' in texto and compra.pedido_principal.codigo in texto
    assert chave == f'kit_preso_gateway|{compra.id}'
    assert gateway.alerta.call_args.kwargs == {'critico': False, 'cooldown': 86400}


def test_erro_numa_compra_fica_isolado_e_volta_no_ciclo_seguinte(compra, gateway, monkeypatch):
    _vencida(compra, pix_expira_em=None)
    original = loja_pagamento.situacao_no_gateway
    chamadas = []

    def falhar_uma_vez(pedido, **kwargs):
        chamadas.append(pedido.id)
        if len(chamadas) == 1:
            raise RuntimeError('Falha de teste')
        return original(pedido, **kwargs)

    monkeypatch.setattr(loja_pagamento, 'situacao_no_gateway', falhar_uma_vez)
    monkeypatch.setattr(pagarme, 'consultar_order', Mock(return_value={
        'ok': True, 'pago': False, 'status': 'failed', 'charge_status': 'failed'}))
    assert kits_estoque.expirar_compras() == []
    assert all(p.status == 'aguardando_pagamento' for p in _pedidos(compra))
    # Ciclo seguinte: a mesma compra é resolvida normalmente.
    assert len(kits_estoque.expirar_compras()) == 2


# ── Entrada malformada (auditoria, item 34) ─────────────────────────────────

@pytest.mark.loja_host
@pytest.mark.parametrize('agenda_json', [
    '[' * 5000 + ']' * 5000,          # aninhado: RecursionError no json.loads
    '{"data": "2026-10-01"}',          # objeto em vez de lista
    '[1, null, "x", [["y"]]]',         # itens que não são data/janela
])
def test_agenda_forjada_responde_400_sem_500(app, kit, frete, checkout, agenda_json):
    cliente, token, _ = _abrir(app, kit)
    resposta = cliente.post(f'/loja/kits-cafe/{kit.id}',
                            data=_form(checkout_token=token, agenda_json=agenda_json))
    assert resposta.status_code == 400
    assert CompraKit.query.count() == 0
    html = resposta.get_data(as_text=True)
    assert '[["y"]]' not in html

"""NF do site: uma hora antes de cada entrega e DANFE com retry independente."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import (
    Cliente,
    CompraKit,
    EntregaKit,
    KitCafe,
    KitCafeItem,
    PagamentoOnline,
    PedidoOnline,
    PedidoOnlineItem,
    Produto,
    ReembolsoKit,
    TarefaFiscalKit,
    TarefaFiscalPedido,
)
from app.services import loja_fiscal, loja_pagamento, tiny, tiny_nf

BASE = datetime(2026, 9, 15, 7)


@pytest.fixture(autouse=True)
def servicos(app, monkeypatch):
    relogio = {'agora': BASE}
    incluir = Mock(side_effect=lambda _payload, **_kwargs: {
        'ok': True, 'id': f'nf-{incluir.call_count}'})
    emitir = Mock(return_value={'ok': True, 'status': 'autorizada'})
    obter = Mock(return_value={})
    email = Mock(return_value={'ok': True})
    monkeypatch.setattr(loja_fiscal, 'agora', lambda: relogio['agora'])
    monkeypatch.setattr(tiny_nf, 'agora', lambda: relogio['agora'])
    monkeypatch.setattr(loja_pagamento, 'agora', lambda: relogio['agora'])
    monkeypatch.setattr(tiny, 'incluir_nota_fiscal', incluir)
    monkeypatch.setattr(tiny, 'emitir_nota_fiscal', emitir)
    monkeypatch.setattr(tiny, 'obter_nota_fiscal', obter)
    monkeypatch.setattr('app.services.email.enviar_nf_emitida', email)
    monkeypatch.setattr('app.services.email.disponivel', lambda: True)
    return SimpleNamespace(relogio=relogio, incluir=incluir, emitir=emitir,
                           obter=obter, email=email)


def _rodar(servicos, instante, **kwargs):
    servicos.relogio['agora'] = loja_fiscal._brt(instante)
    return loja_fiscal.processar_pendentes(base=instante, **kwargs)


def _pedido(*, data=None, janela='09:00–10:00', modo='agendada', status='pago',
            pago_em=BASE - timedelta(days=1), fila=True, frete='3.40', divulgacao=False):
    produto = Produto.query.first()
    if not produto:
        produto = Produto(nome='Café da manhã', ativo=True, site_ativo=True, preco_site=10)
        db.session.add(produto)
        db.session.flush()
        tiny_nf.definir_sku('produto', produto.id, 'CAFE-001')
    cliente = Cliente.query.first()
    if not cliente:
        cliente = Cliente(nome='Maria Cliente', email='maria@example.com',
                           cpf='52998224725', telefone='11999998888')
        db.session.add(cliente)
        db.session.flush()
    pedido = PedidoOnline(
        cliente_id=cliente.id, nome_cliente=cliente.nome, email_cliente=cliente.email,
        telefone_cliente=cliente.telefone, modo_entrega=modo,
        data_entrega=data or BASE.date(), janela_entrega=janela,
        status=status, pago_em=pago_em, divulgacao=divulgacao,
        endereco_logradouro='Rua Teste', endereco_numero='10', endereco_bairro='Moema',
        endereco_cidade='São Paulo', endereco_uf='SP', endereco_cep='04077000',
        subtotal=Decimal('20.00'), frete_valor=Decimal(frete),
        valor_total=Decimal('20.00') + Decimal(frete))
    pedido.itens.append(PedidoOnlineItem(
        kind='produto', produto_id=produto.id, nome=produto.nome,
        quantidade=2, preco_unitario=Decimal('10.00'), subtotal=Decimal('20.00')))
    db.session.add(pedido)
    db.session.flush()
    if fila:
        loja_fiscal.agendar(pedido, base=BASE)
    db.session.commit()
    return pedido


def _grupo(owner):
    pedidos = [_pedido(data=BASE.date() + timedelta(days=7 * i), frete=frete)
               for i, frete in enumerate(('3.40', '5.50', '7.80'))]
    kit = KitCafe(nome='Café com três entregas', ativo=True, usuario_id=owner.id)
    kit.itens.append(KitCafeItem(kind='produto', produto_id=pedidos[0].itens[0].produto_id,
                                 quantidade=2))
    db.session.add(kit)
    db.session.flush()
    compra = CompraKit(kit_id=kit.id, kit_nome=kit.nome, pedido_principal_id=pedidos[0].id,
                        subtotal=Decimal('60.00'), frete_total=Decimal('16.70'),
                        valor_total=Decimal('76.70'), pago_em=BASE - timedelta(days=1),
                        expira_em=BASE, checkout_token='c' * 64)
    db.session.add(compra)
    db.session.flush()
    for ordem, pedido in enumerate(pedidos, 1):
        db.session.add(EntregaKit(compra_id=compra.id, pedido_id=pedido.id, ordem=ordem))
    db.session.commit()
    return compra, pedidos


@pytest.mark.parametrize('janela,esperado', [
    ('09:00–10:00', datetime(2026, 9, 15, 8)),
    ('09:30-10:45', datetime(2026, 9, 15, 8, 30)),
    ('08h–12h', datetime(2026, 9, 15, 7)),
    ('10h30 às 12h', datetime(2026, 9, 15, 9, 30)),
    ('09:15', datetime(2026, 9, 15, 8, 15)),
    ('6:00 — 10:00', datetime(2026, 9, 15, 5)),
])
def test_horario_usa_inicio_da_janela_com_minutos(app, janela, esperado):
    pedido = _pedido(janela=janela)
    assert loja_fiscal.horario_emissao(pedido) == esperado


def test_nao_emite_um_segundo_antes_e_emite_na_fronteira(app, servicos):
    pedido = _pedido()
    assert _rodar(servicos, datetime(2026, 9, 15, 7, 59, 59))['processados'] == 0
    servicos.incluir.assert_not_called()
    servicos.email.assert_not_called()
    assert _rodar(servicos, datetime(2026, 9, 15, 8))['concluidos'] == 1
    assert pedido.nf_emitida_em == datetime(2026, 9, 15, 8)
    assert db.session.get(TarefaFiscalPedido, pedido.id).email_enviado_em == datetime(2026, 9, 15, 8)
    servicos.incluir.assert_called_once()
    servicos.email.assert_called_once_with(pedido)


def test_base_utc_aware_e_convertida_para_brt(app, servicos):
    _pedido(janela='09:30–10:30')
    antes = datetime(2026, 9, 15, 11, 29, 59, tzinfo=UTC)
    assert _rodar(servicos, antes)['processados'] == 0
    assert _rodar(servicos, datetime(2026, 9, 15, 11, 30, tzinfo=UTC))['concluidos'] == 1


def test_horario_proximo_da_meia_noite_emite_na_vespera(app, servicos):
    pedido = _pedido(data=BASE.date() + timedelta(days=1), janela='00:30–01:30')
    assert loja_fiscal.horario_emissao(pedido) == datetime(2026, 9, 15, 23, 30)
    assert _rodar(servicos, datetime(2026, 9, 15, 23, 29))['processados'] == 0
    assert _rodar(servicos, datetime(2026, 9, 15, 23, 30))['concluidos'] == 1


def test_tres_datas_de_kit_emitem_notas_individuais_apenas_no_dia_certo(owner_user, servicos):
    compra, pedidos = _grupo(owner_user)
    for i, pedido in enumerate(pedidos):
        instante = datetime.combine(pedido.data_entrega, datetime.min.time()).replace(hour=8)
        assert _rodar(servicos, instante - timedelta(seconds=1))['processados'] == 0
        assert _rodar(servicos, instante)['concluidos'] == 1
        assert servicos.incluir.call_count == i + 1
        assert servicos.email.call_count == i + 1
        payload = servicos.incluir.call_args.args[0]
        assert payload['valor_frete'] == float(pedido.frete_valor)
        assert payload['itens'][0]['item']['quantidade'] == 2.0
        assert payload['itens'][0]['item']['valor_unitario'] == 10.0
        assert all(p.nf_emitida_em is None for p in pedidos[i + 1:])
    assert compra.valor_total == Decimal('76.70')
    assert all(p.valor_total == Decimal('20.00') + p.frete_valor for p in pedidos)


@pytest.mark.parametrize('janela', ['em até 1h', 'em até 2h'])
def test_express_emite_assim_que_pago(app, servicos, janela):
    pago_em = datetime(2026, 9, 15, 7, 32)
    pedido = _pedido(modo='express', janela=janela, pago_em=pago_em)
    assert loja_fiscal.horario_emissao(pedido) == pago_em
    assert _rodar(servicos, pago_em)['concluidos'] == 1
    servicos.email.assert_called_once_with(pedido)


def test_retirada_respeita_horario_marcado(app, servicos):
    _pedido(modo='retirada', janela='10:30–11:30')
    assert _rodar(servicos, datetime(2026, 9, 15, 9, 29))['processados'] == 0
    assert _rodar(servicos, datetime(2026, 9, 15, 9, 30))['concluidos'] == 1


def test_pagamento_menos_de_uma_hora_antes_emite_sem_esperar_outro_dia(app, servicos):
    pago_em = datetime(2026, 9, 15, 8, 45)
    _pedido(pago_em=pago_em)
    assert _rodar(servicos, pago_em)['concluidos'] == 1


def test_antecipar_agenda_recalcula_mesmo_com_tarefa_agendada_para_mais_tarde(app, servicos):
    pedido = _pedido(janela='16:00–17:00')
    assert _rodar(servicos, datetime(2026, 9, 15, 8))['processados'] == 0
    assert db.session.get(TarefaFiscalPedido, pedido.id).proxima_tentativa_em.hour == 15
    pedido.janela_entrega = '09:30–10:30'
    db.session.commit()
    assert _rodar(servicos, datetime(2026, 9, 15, 8, 30))['concluidos'] == 1


def test_adiar_agenda_nao_emite_no_horario_antigo(app, servicos):
    pedido = _pedido()
    pedido.janela_entrega = '11:00–12:00'
    db.session.commit()
    assert _rodar(servicos, datetime(2026, 9, 15, 8))['processados'] == 0
    servicos.incluir.assert_not_called()
    assert _rodar(servicos, datetime(2026, 9, 15, 10))['concluidos'] == 1


def test_mudanca_para_outro_dia_preserva_nova_data(app, servicos):
    pedido = _pedido()
    pedido.data_entrega += timedelta(days=2)
    db.session.commit()
    assert _rodar(servicos, datetime(2026, 9, 15, 8))['processados'] == 0
    assert _rodar(servicos, datetime(2026, 9, 17, 8))['concluidos'] == 1


@pytest.mark.parametrize('status', ['pago', 'em_preparo', 'a_caminho'])
def test_recupera_pedido_pago_sem_tarefa_inclusive_em_preparo(app, servicos, status):
    pedido = _pedido(fila=False, status=status)
    assert TarefaFiscalPedido.query.count() == 0
    assert _rodar(servicos, datetime(2026, 9, 15, 8))['concluidos'] == 1
    assert db.session.get(TarefaFiscalPedido, pedido.id).concluido_em


def test_entregue_historico_sem_tarefa_nao_e_refaturado(app, servicos):
    _pedido(data=BASE.date() - timedelta(days=40), status='entregue', fila=False)
    assert _rodar(servicos, BASE)['processados'] == 0
    assert TarefaFiscalPedido.query.count() == 0
    servicos.incluir.assert_not_called()
    servicos.email.assert_not_called()


def test_nf_antiga_sem_fila_nao_e_recriada_nem_reenviada(app, servicos):
    pedido = _pedido(fila=False)
    pedido.tiny_nota_fiscal_id = 'nf-antiga'
    pedido.nf_emitida_em = BASE - timedelta(days=1)
    db.session.commit()
    assert _rodar(servicos, BASE)['processados'] == 0
    assert TarefaFiscalPedido.query.count() == 0
    assert pedido.tiny_nota_fiscal_id == 'nf-antiga'
    servicos.incluir.assert_not_called()
    servicos.email.assert_not_called()


def test_tarefa_antiga_do_kit_conserva_registro_de_email_enviado(app, servicos):
    pedido = _pedido()
    tarefa = db.session.get(TarefaFiscalKit, pedido.id)
    tarefa.email_enviado_em = BASE - timedelta(days=1)
    tarefa.concluido_em = BASE - timedelta(days=1)
    pedido.tiny_nota_fiscal_id = 'nf-kit-antiga'
    pedido.nf_emitida_em = BASE - timedelta(days=1)
    db.session.commit()
    assert db.session.get(TarefaFiscalPedido, pedido.id) is tarefa
    assert _rodar(servicos, datetime(2026, 9, 15, 8))['processados'] == 0
    servicos.incluir.assert_not_called()
    servicos.email.assert_not_called()


@pytest.mark.parametrize('status,pago_em,divulgacao', [
    ('cancelado', BASE - timedelta(days=1), False),
    ('aguardando_pagamento', None, False),
    ('pago', None, False),
    ('divulgacao', None, True),
    ('pago', BASE - timedelta(days=1), True),
])
def test_sem_venda_paga_ativa_nao_emite(app, servicos, status, pago_em, divulgacao):
    pedido = _pedido(status=status, pago_em=pago_em, divulgacao=divulgacao)
    assert _rodar(servicos, datetime(2026, 9, 15, 8))['ignorados'] == 1
    assert db.session.get(TarefaFiscalPedido, pedido.id).concluido_em
    servicos.incluir.assert_not_called()
    servicos.email.assert_not_called()


@pytest.mark.parametrize('janela', ['', 'manhã', '25:00–26:00', '10:00–09:00', '08:90–09:00'])
def test_janela_ilegivel_fica_pendente_com_orientacao(app, servicos, janela):
    pedido = _pedido(janela=janela)
    assert loja_fiscal.horario_emissao(pedido) is None
    assert _rodar(servicos, datetime(2026, 9, 15, 12))['concluidos'] == 0
    tarefa = db.session.get(TarefaFiscalPedido, pedido.id)
    assert not tarefa.concluido_em and 'horário' in tarefa.erro
    servicos.incluir.assert_not_called()


def test_data_ausente_fica_pendente_sem_horario_inventado(app, servicos):
    pedido = _pedido()
    pedido.data_entrega = None
    db.session.commit()
    assert loja_fiscal.horario_emissao(pedido) is None
    assert _rodar(servicos, BASE)['processados'] == 0
    assert db.session.get(TarefaFiscalPedido, pedido.id).erro
    servicos.incluir.assert_not_called()


def test_retry_email_nao_reemite_nem_espera_agenda_alterada(app, servicos):
    pedido = _pedido()
    servicos.email.return_value = {'ok': False, 'erro': 'E-mail temporariamente indisponível'}
    assert _rodar(servicos, datetime(2026, 9, 15, 8))['pendentes'] == 1
    tarefa = db.session.get(TarefaFiscalPedido, pedido.id)
    assert pedido.nf_emitida_em and not tarefa.email_enviado_em
    assert 'NF emitida' in tarefa.erro
    pedido.data_entrega += timedelta(days=7)
    db.session.commit()
    servicos.email.return_value = {'ok': True}
    assert _rodar(servicos, datetime(2026, 9, 15, 8, 4))['processados'] == 0
    assert _rodar(servicos, datetime(2026, 9, 15, 8, 5))['concluidos'] == 1
    servicos.incluir.assert_called_once()
    servicos.emitir.assert_called_once()
    assert servicos.email.call_count == 2


def test_pagamento_persiste_apenas_agendamento_sem_chamar_tiny(app, servicos):
    pedido = _pedido(status='aguardando_pagamento', pago_em=None, fila=False)
    pagamento = PagamentoOnline(pedido_id=pedido.id, metodo='pix', valor=pedido.valor_total,
                                 status='pendente')
    db.session.add(pagamento)
    db.session.commit()
    assert loja_pagamento._marcar_pago(pedido, pagamento, enviar_confirmacao=False)
    db.session.commit()
    tarefa = db.session.get(TarefaFiscalPedido, pedido.id)
    assert tarefa and tarefa.proxima_tentativa_em == datetime(2026, 9, 15, 8)
    assert pedido.status == 'pago' and pagamento.status == 'pago'
    servicos.incluir.assert_not_called()
    servicos.email.assert_not_called()


def test_falha_commit_pagamento_desfaz_tarefa_e_status(app, servicos, monkeypatch):
    pedido = _pedido(status='aguardando_pagamento', pago_em=None, fila=False)
    pagamento = PagamentoOnline(pedido_id=pedido.id, metodo='pix', valor=pedido.valor_total,
                                 status='pendente')
    db.session.add(pagamento)
    db.session.commit()
    assert loja_pagamento._marcar_pago(pedido, pagamento, enviar_confirmacao=False)
    original_commit = db.session.commit
    monkeypatch.setattr(db.session, 'commit', Mock(side_effect=SQLAlchemyError('Falha simulada')))
    with pytest.raises(SQLAlchemyError):
        db.session.commit()
    db.session.rollback()
    monkeypatch.setattr(db.session, 'commit', original_commit)
    db.session.refresh(pedido)
    db.session.refresh(pagamento)
    assert pedido.status == 'aguardando_pagamento' and pedido.pago_em is None
    assert pagamento.status == 'pendente'
    assert TarefaFiscalPedido.query.count() == 0
    servicos.incluir.assert_not_called()
    servicos.email.assert_not_called()


@pytest.mark.parametrize('status', ['solicitado', 'confirmado'])
def test_reembolso_com_pedido_ainda_pago_impede_nova_nf(app, servicos, status):
    pedido = _pedido()
    db.session.add(ReembolsoKit(pedido_id=pedido.id, valor=pedido.valor_total,
                                pagarme_charge_id='ch-teste', status=status))
    db.session.commit()
    assert _rodar(servicos, datetime(2026, 9, 15, 8))['pendentes'] == 1
    assert 'reembolso' in db.session.get(TarefaFiscalPedido, pedido.id).erro
    servicos.incluir.assert_not_called()


def test_tarefas_futuras_nao_ocupam_limite_das_notas_vencidas(app, servicos):
    futuros = [_pedido(janela='17:00–18:00') for _ in range(3)]
    vencido = _pedido(janela='09:00–10:00')
    assert _rodar(servicos, datetime(2026, 9, 15, 8), limite=1)['concluidos'] == 1
    assert vencido.nf_emitida_em and all(not p.nf_emitida_em for p in futuros)


@pytest.mark.parametrize('falha', ['incerto', 'queda'])
def test_inclusao_sem_confirmacao_nao_cria_outra_nota(servicos, falha):
    pedido = _pedido()
    if falha == 'queda':
        servicos.incluir.side_effect = RuntimeError('conexão interrompida')
    else:
        servicos.incluir.side_effect = None
        servicos.incluir.return_value = {'ok': False, 'incerto': True, 'erro': 'timeout'}
    _rodar(servicos, BASE + timedelta(hours=1))
    assert pedido.nf_status == 'inclusao_iniciada'
    assert not pedido.tiny_nota_fiscal_id
    _rodar(servicos, BASE + timedelta(hours=1, minutes=5))
    assert not tiny_nf.emitir_nf(pedido, recriar=True)['ok']
    servicos.incluir.assert_called_once()
    servicos.emitir.assert_not_called()


def test_owner_resolve_ausencia_de_nota_sem_emitir_antes_do_horario(owner_user, servicos):
    pedido = _pedido()
    pedido.nf_status = 'inclusao_iniciada'
    db.session.commit()
    assert not tiny_nf.resolver_inclusao_incerta(pedido, user_id=owner_user.id)['ok']
    assert tiny_nf.resolver_inclusao_incerta(pedido, user_id=owner_user.id, sem_nota=True)['ok']
    assert pedido.nf_status is None
    servicos.incluir.assert_not_called()
    assert _rodar(servicos, BASE)['processados'] == 0
    assert _rodar(servicos, BASE + timedelta(hours=1))['concluidos'] == 1
    servicos.incluir.assert_called_once()


def test_owner_vincula_nota_existente_e_fila_envia_sem_criar_outra(owner_user, servicos):
    pedido = _pedido()
    pedido.nf_status = 'inclusao_iniciada'
    db.session.commit()
    servicos.obter.return_value = {'id': '12345', 'situacao': 'autorizada'}
    assert tiny_nf.resolver_inclusao_incerta(pedido, user_id=owner_user.id, nota_id='12345')['ok']
    assert pedido.tiny_nota_fiscal_id == '12345' and pedido.nf_emitida_em
    assert _rodar(servicos, BASE + timedelta(hours=1))['concluidos'] == 1
    servicos.incluir.assert_not_called()
    servicos.emitir.assert_not_called()
    servicos.email.assert_called_once()


def test_somente_owner_resolve_inclusao_incerta(admin_user, servicos):
    pedido = _pedido()
    pedido.nf_status = 'inclusao_iniciada'
    db.session.commit()
    assert not tiny_nf.resolver_inclusao_incerta(pedido, user_id=admin_user.id, sem_nota=True)['ok']
    assert pedido.nf_status == 'inclusao_iniciada'
    servicos.incluir.assert_not_called()


def test_estorno_recebido_apos_inclusao_impede_autorizacao(servicos):
    pedido = _pedido()

    def incluir(_payload, **kwargs):
        pedido.status = 'cancelado'
        db.session.commit()
        return {'ok': True, 'id': '12345'}

    servicos.incluir.side_effect = incluir
    _rodar(servicos, BASE + timedelta(hours=1))
    assert pedido.tiny_nota_fiscal_id == '12345'
    assert not pedido.nf_emitida_em
    servicos.emitir.assert_not_called()
    servicos.email.assert_not_called()


def test_tela_owner_confere_pendencia_e_exige_confirmacao(app, owner_user, servicos):
    pedido = _pedido()
    pedido.nf_status = 'inclusao_iniciada'
    db.session.commit()
    cliente = app.test_client()
    with cliente.session_transaction() as sessao:
        sessao['_user_id'] = str(owner_user.id)
        sessao['_fresh'] = True
    rota = f'/admin/loja-online/pedidos/{pedido.codigo}'
    resposta = cliente.get(rota)
    assert resposta.status_code == 200
    assert 'Registrar conferência e retomar' in resposta.get_data(as_text=True)
    assert '15/09 às 08:00' in resposta.get_data(as_text=True)
    assert cliente.post(rota + '/resolver-nf', data={'sem_nota': '1'}).status_code == 302
    assert pedido.nf_status == 'inclusao_iniciada'
    assert cliente.post(rota + '/resolver-nf', data={'sem_nota': '1', 'conferido': '1'}).status_code == 302
    assert pedido.nf_status is None
    servicos.incluir.assert_not_called()

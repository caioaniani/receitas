"""Recebimento direto confirmado pelo owner: dinheiro, permissões e estoque."""
from decimal import Decimal
from unittest.mock import Mock

import pytest
from sqlalchemy import event, text
from test_menu_configuravel import _menu, _pedido_com_menu, _pis, _site_loja

from app.extensions import db
from app.models import (
    EstoqueLoja,
    EstoqueProducao,
    EstoqueSitePlano,
    MovEstoqueLoja,
    MovEstoqueProducao,
    PagamentoExternoOnline,
    PagamentoOnline,
    PedidoOnline,
    PedidoOnlineItem,
    Receita,
    SaidaProducaoSite,
)
from app.services import loja_estoque_reserva, loja_pagamento, pagamento_externo
from app.utils import agora, hoje


@pytest.fixture(autouse=True)
def efeitos_externos(app, monkeypatch):
    """Não envia comunicação real nem permite movimentar dinheiro no gateway."""
    monkeypatch.setattr('app.services.email.disponivel', lambda: False)
    mocks = {}
    for nome in ('_enviar_confirmacao', '_reportar_purchase'):
        mocks[nome] = Mock()
        monkeypatch.setattr(loja_pagamento, nome, mocks[nome])
    gateway = []
    for nome in ('criar_pedido_pix', 'criar_pedido_cartao', 'cancelar_charge',
                 'consultar_order'):
        bloqueado = Mock(side_effect=AssertionError('A confirmação externa não chama o gateway'))
        monkeypatch.setattr(loja_pagamento.pagarme, nome, bloqueado)
        gateway.append(bloqueado)
    yield mocks
    for bloqueado in gateway:
        bloqueado.assert_not_called()


@pytest.fixture
def pedido(app, loja):
    receita = Receita(nome='Pão de teste', categoria='Paes', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100)
    db.session.add(receita)
    db.session.flush()
    db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=receita.id, quantidade=10))
    db.session.add(EstoqueSitePlano(kind='receita', item_id=receita.id, data=hoje(),
                                   qtd_planejada=20, qtd_reservada=0))
    p = PedidoOnline(nome_cliente='Maria', email_cliente='maria@example.com',
                     modo_entrega='retirada', loja_retirada_id=loja.id,
                     data_entrega=hoje(), subtotal=Decimal('40.00'),
                     frete_valor=Decimal('10.00'), valor_total=Decimal('50.00'))
    p.itens.append(PedidoOnlineItem(kind='receita', receita_id=receita.id,
                                    nome=receita.nome, quantidade=2,
                                    preco_unitario=Decimal('20.00'), subtotal=Decimal('40.00')))
    db.session.add(p)
    db.session.flush()
    db.session.add(PagamentoOnline(pedido_id=p.id, metodo='pix', valor=Decimal('50.00'),
                                  pagarme_order_id='or_qr_antigo',
                                  pagarme_charge_id='ch_qr_antigo'))
    loja_estoque_reserva.reservar(p, loja_id=loja.id)
    db.session.commit()
    return p


def _confirmar(p, owner, **mudancas):
    dados = {'usuario_id': owner.id, 'referencia': 'Pix Maria, conferido no extrato',
             'valor_recebido': '50,00', 'confirmado': True}
    dados.update(mudancas)
    return pagamento_externo.confirmar_recebimento(p, **dados)


def _cliente(app, usuario=None):
    c = app.test_client()
    if usuario:
        with c.session_transaction() as s:
            s['_user_id'] = str(usuario.id)
            s['_fresh'] = True
    return c


def _post(c, p, **mudancas):
    dados = {'referencia': 'Pix Maria, conferido no extrato',
             'valor_recebido': '50,00', 'confirmado': '1'}
    dados.update(mudancas)
    return c.post(f'/admin/loja-online/pedidos/{p.codigo}/confirmar-pagamento-externo',
                  data=dados)


def _nenhum_recebimento(p, efeitos):
    assert p.pago_em is None
    assert PagamentoExternoOnline.query.count() == 0
    assert PagamentoOnline.query.filter_by(metodo='externo').count() == 0
    assert EstoqueLoja.query.one().quantidade == 10
    assert MovEstoqueLoja.query.count() == 0
    assert EstoqueSitePlano.query.one().qtd_reservada == 0
    for efeito in efeitos.values():
        efeito.assert_not_called()


def test_owner_confirma_valor_integral_e_preserva_tentativa_qr(
        pedido, owner_user, efeitos_externos):
    def conferir_commit(_):
        # A comunicação só pode começar quando OUTRA conexão já enxerga a auditoria.
        with db.engine.connect() as conexao:
            assert conexao.execute(text('SELECT COUNT(*) FROM pagamento_externo_online')).scalar() == 1

    efeitos_externos['_enviar_confirmacao'].side_effect = conferir_commit
    assert _confirmar(pedido, owner_user)[0]
    assert pedido.status == 'pago' and pedido.pago_em
    auditado = db.session.get(PagamentoExternoOnline, pedido.id)
    pagamento = db.session.get(PagamentoOnline, auditado.pagamento_id)
    assert auditado.usuario_id == owner_user.id
    assert auditado.usuario.nome == 'dono teste'
    assert auditado.valor == pagamento.valor == Decimal('50.00')
    assert auditado.confirmado_em == pedido.pago_em == pagamento.pago_em
    assert auditado.referencia == 'Pix Maria, conferido no extrato'
    assert pagamento.status == 'pago' and pagamento.metodo == 'externo'
    assert pagamento.pagarme_order_id is None and pagamento.pagarme_charge_id is None
    antigo = PagamentoOnline.query.filter_by(metodo='pix').one()
    assert antigo.status == 'pendente'
    assert antigo.pagarme_order_id == 'or_qr_antigo'
    assert antigo.pagarme_charge_id == 'ch_qr_antigo'
    estoque = EstoqueLoja.query.one()
    assert estoque.quantidade == 8 and estoque.quantidade_reservada == 0
    assert pedido.reserva_expira_em is None
    mov = MovEstoqueLoja.query.filter_by(tipo='venda_site').one()
    assert mov.quantidade == 2 and mov.usuario_id == owner_user.id
    assert EstoqueSitePlano.query.one().qtd_reservada == 2
    for efeito in efeitos_externos.values():
        efeito.assert_called_once_with(pedido)


def test_repeticao_post_nao_duplica_pagamento_baixa_plano_ou_notificacao(
        app, pedido, owner_user, efeitos_externos):
    c = _cliente(app, owner_user)
    assert _post(c, pedido).status_code == 302
    instante = pedido.pago_em
    assert _post(c, pedido, referencia='Outra referência em duplo clique').status_code == 302
    assert pedido.pago_em == instante
    assert PagamentoExternoOnline.query.count() == 1
    assert PagamentoExternoOnline.query.one().referencia == 'Pix Maria, conferido no extrato'
    assert PagamentoOnline.query.filter_by(metodo='externo').count() == 1
    assert EstoqueLoja.query.one().quantidade == 8
    assert MovEstoqueLoja.query.filter_by(tipo='venda_site').count() == 1
    assert EstoqueSitePlano.query.one().qtd_reservada == 2
    for efeito in efeitos_externos.values():
        efeito.assert_called_once()


@pytest.mark.parametrize('autenticado', [False, True])
def test_rota_bloqueia_anonimo_e_admin_sem_owner(
        app, pedido, admin_user, autenticado, efeitos_externos):
    c = _cliente(app, admin_user if autenticado else None)
    assert _post(c, pedido).status_code in (302, 403)
    _nenhum_recebimento(pedido, efeitos_externos)


@pytest.mark.parametrize('usuario', ['admin', 'ausente', 'treino'])
def test_servico_tambem_exige_owner_real(
        pedido, owner_user, admin_user, usuario, efeitos_externos):
    usuario_id = {'admin': admin_user.id, 'ausente': None, 'treino': owner_user.id}[usuario]
    if usuario == 'treino':
        owner_user.somente_treino = True
        db.session.commit()
    assert not _confirmar(pedido, owner_user, usuario_id=usuario_id)[0]
    _nenhum_recebimento(pedido, efeitos_externos)


@pytest.mark.parametrize('dados', [
    {'valor_recebido': '40,00'},  # subtotal não cobre o frete
    {'valor_recebido': '49,99'},
    {'valor_recebido': '50,01'},
    {'valor_recebido': '50,001'},
    {'valor_recebido': 'NaN'},
    {'valor_recebido': 'Infinity'},
    {'valor_recebido': '5e1'},
    {'valor_recebido': '-50'},
    {'valor_recebido': '0'},
    {'valor_recebido': ''},
    {'referencia': '   '},
    {'referencia': 'x' * 201},
    {'confirmado': False},
    {'confirmado': '1'},  # o serviço exige a confirmação booleana da rota
])
def test_validacao_nao_mexe_em_dinheiro_estoque_ou_plano(
        pedido, owner_user, dados, efeitos_externos):
    assert not _confirmar(pedido, owner_user, **dados)[0]
    assert pedido.status == 'aguardando_pagamento'
    assert EstoqueLoja.query.one().quantidade_reservada == 2
    _nenhum_recebimento(pedido, efeitos_externos)


@pytest.mark.parametrize('valor', ['50', '50.00', '50,00', ' 50,00 '])
def test_formatos_de_valor_exato(pedido, owner_user, valor):
    assert _confirmar(pedido, owner_user, valor_recebido=valor)[0]
    assert PagamentoExternoOnline.query.one().valor == Decimal('50.00')


def test_qr_expirado_reabre_pedido_e_baixa_sem_reserva(pedido, owner_user):
    loja_estoque_reserva.liberar(pedido, loja_id=pedido.loja_retirada_id)
    pedido.status = 'cancelado'
    pedido.motivo_cancelamento = 'pix_expirado'
    pedido.cancelado_em = agora()
    db.session.commit()
    assert pagamento_externo.pode_confirmar(pedido)
    assert _confirmar(pedido, owner_user)[0]
    assert pedido.status == 'pago'
    assert pedido.motivo_cancelamento is None and pedido.cancelado_em is None
    assert EstoqueLoja.query.one().quantidade == 8
    assert EstoqueLoja.query.one().quantidade_reservada == 0
    assert EstoqueSitePlano.query.one().qtd_reservada == 2


@pytest.mark.parametrize('caso', ['cancelado_admin', 'reembolso', 'cancelado_legado',
                                'divulgacao', 'pago', 'em_preparo', 'a_caminho', 'entregue'])
def test_pedidos_incompativeis_nao_podem_ser_liberados(
        pedido, owner_user, caso, efeitos_externos):
    if caso == 'divulgacao':
        pedido.divulgacao = True
    elif caso in ('cancelado_admin', 'reembolso', 'cancelado_legado'):
        pedido.status = 'cancelado'
        pedido.motivo_cancelamento = None if caso == 'cancelado_legado' else caso
        pedido.cancelado_em = agora()
    else:
        pedido.status = caso
    db.session.commit()
    assert not pagamento_externo.pode_confirmar(pedido)
    assert not _confirmar(pedido, owner_user)[0]
    _nenhum_recebimento(pedido, efeitos_externos)


def test_gateway_ja_pago_impede_segundo_recebimento(pedido, owner_user, efeitos_externos):
    PagamentoOnline.query.one().status = 'pago'
    db.session.commit()
    assert not _confirmar(pedido, owner_user)[0]
    _nenhum_recebimento(pedido, efeitos_externos)


def test_falha_ao_gravar_auditoria_reverte_pagamento_estoque_e_plano(
        pedido, owner_user, efeitos_externos):
    def falhar(*_):
        raise RuntimeError('Falha simulada ao persistir auditoria')

    event.listen(PagamentoExternoOnline, 'before_insert', falhar)
    try:
        assert not _confirmar(pedido, owner_user)[0]
    finally:
        event.remove(PagamentoExternoOnline, 'before_insert', falhar)
    db.session.refresh(pedido)
    assert pedido.status == 'aguardando_pagamento'
    assert pedido.reserva_expira_em is not None
    assert EstoqueLoja.query.one().quantidade_reservada == 2
    _nenhum_recebimento(pedido, efeitos_externos)


@pytest.mark.parametrize('owner', [True, False])
def test_tela_apresenta_acao_somente_ao_owner(app, pedido, owner_user, admin_user, owner):
    rota = f'/admin/loja-online/pedidos/{pedido.codigo}'
    resposta = _cliente(app, owner_user if owner else admin_user).get(rota)
    assert resposta.status_code == 200
    html = resposta.get_data(as_text=True)
    if owner:
        assert f'{rota}/confirmar-pagamento-externo' in html
        assert 'name="valor_recebido"' in html and 'value="50,00"' in html
        assert 'name="referencia"' in html and 'name="confirmado"' in html
    else:
        assert f'{rota}/confirmar-pagamento-externo' not in html


def test_tela_embed_preserva_contexto_e_mostra_historico_apos_confirmar(
        app, pedido, owner_user):
    c = _cliente(app, owner_user)
    rota = f'/admin/loja-online/pedidos/{pedido.codigo}'
    antes = c.get(rota + '?embed=1').get_data(as_text=True)
    assert 'name="embed" value="1"' in antes
    resposta = _post(c, pedido, embed='1')
    assert resposta.status_code == 302
    assert resposta.location.endswith(f'{rota}?embed=1')
    depois = c.get(resposta.location).get_data(as_text=True)
    assert 'dono teste' in depois and 'Pix Maria, conferido no extrato' in depois
    assert 'Externo (Pix/transferência direta)' in depois
    assert 'R$ 50,00' in depois
    assert 'name="confirmado"' not in depois
    assert '/reduzir-item' not in depois
    assert f'{rota}/cancelar' not in depois
    assert 'Pagar.me' in depois


def test_tela_avisa_reabertura_do_qr_expirado(app, pedido, owner_user):
    pedido.status = 'cancelado'
    pedido.motivo_cancelamento = 'pix_expirado'
    pedido.cancelado_em = agora()
    db.session.commit()
    resposta = _cliente(app, owner_user).get(f'/admin/loja-online/pedidos/{pedido.codigo}')
    assert resposta.status_code == 200
    assert 'Confirmar o recebimento reabre este pedido como pago' in resposta.get_data(as_text=True)


def test_mini_sob_encomenda_pago_externo_so_baixa_industria_na_coleta(app, owner_user):
    from app.services import loja_entrega, saida_producao_site

    loja = _site_loja(db)
    menu, minis = _menu(db)
    menu.sob_encomenda = True
    for mini in minis:
        db.session.add(EstoqueProducao(receita_id=mini.id, quantidade=50))
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=mini.id, quantidade=50))
    db.session.commit()
    a, b, c = _pis(menu)
    p = _pedido_com_menu(db, menu, {a: 10, b: 3, c: 2}, loja=loja, qtd=2)
    p.data_entrega = hoje()
    db.session.commit()
    assert _confirmar(p, owner_user, valor_recebido='20,00')[0]
    assert [e.quantidade for e in EstoqueLoja.query.order_by(EstoqueLoja.receita_id)] == [50, 50, 50]
    assert [e.quantidade for e in EstoqueProducao.query.order_by(EstoqueProducao.receita_id)] == [50, 50, 50]
    assert MovEstoqueLoja.query.count() == 0 and MovEstoqueProducao.query.count() == 0
    assert EstoqueSitePlano.query.one().qtd_reservada == 2
    assert SaidaProducaoSite.query.count() == 0
    loja_entrega.avancar_status_entrega(p.codigo, 'a_caminho')
    # Contratar/atribuir transporte ainda não é a saída física.
    assert SaidaProducaoSite.query.count() == 0
    saida_producao_site.registrar(p, 'coleta', usuario_id=owner_user.id)
    db.session.commit()
    saida_producao_site.registrar(p, 'retry', usuario_id=owner_user.id)
    db.session.commit()
    assert [e.quantidade for e in EstoqueProducao.query.order_by(EstoqueProducao.receita_id)] == [30, 44, 46]
    assert [e.quantidade for e in EstoqueLoja.query.order_by(EstoqueLoja.receita_id)] == [50, 50, 50]
    assert SaidaProducaoSite.query.count() == 1
    assert MovEstoqueProducao.query.filter_by(tipo='saida_site_direto').count() == 3

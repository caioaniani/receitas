"""Saída física de minis comprados sob encomenda: composição e transação."""
import json
from unittest.mock import patch

import pytest
from test_menu_configuravel import _menu, _pedido_com_menu, _pis, _site_loja

from app.extensions import db
from app.models import (
    AppConfig,
    AtribuicaoEntrega,
    Driver,
    EstoqueLoja,
    EstoqueProducao,
    LalamoveEntrega,
    MovEstoqueProducao,
    PedidoOnlineItem,
    RotaInicio,
    SaidaProducaoSite,
)
from app.services import acerto_despacho, loja_entrega, loja_pagamento, saida_producao_site
from app.services.rastreio_entrega import iniciar_rota
from app.utils import hoje


def _cenario(*, qtd=1, saldo=50, sob=True):
    loja = _site_loja(db)
    menu, minis = _menu(db)
    menu.sob_encomenda = sob
    for mini in minis:
        db.session.add(EstoqueProducao(receita_id=mini.id, quantidade=saldo))
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=mini.id, quantidade=50))
    db.session.commit()
    a, b, c = _pis(menu)
    p = _pedido_com_menu(db, menu, {a: 10, b: 3, c: 2}, loja=loja, qtd=qtd)
    p.data_entrega = hoje()
    db.session.commit()
    with patch('app.services.loja_pagamento._enviar_confirmacao'):
        loja_pagamento._marcar_pago(p, None)
        db.session.commit()
    return p, loja, menu, minis


def _saldos():
    return [e.quantidade for e in EstoqueProducao.query.order_by(EstoqueProducao.receita_id)]


def _saldos_loja():
    return [e.quantidade for e in EstoqueLoja.query.order_by(EstoqueLoja.receita_id)]


def _driver(p):
    d = Driver(nome='Motorista minis', token='minis', pin='1234')
    db.session.add(d)
    db.session.flush()
    db.session.add(AtribuicaoEntrega(driver_id=d.id, pedido_code=p.codigo,
                                    data_entrega=hoje(), ordem=1))
    db.session.commit()
    return d


def test_pagamento_e_contratacao_nao_sao_coleta(app):
    p, _, _, _ = _cenario()
    assert _saldos() == [50, 50, 50]
    assert _saldos_loja() == [50, 50, 50]
    with patch('app.services.email.disponivel', return_value=False):
        loja_entrega.avancar_status_entrega(p.codigo, 'a_caminho')
    assert _saldos() == [50, 50, 50]
    assert SaidaProducaoSite.query.count() == 0


def test_coleta_baixa_escolha_multiplicada_uma_vez(app):
    p, _, menu, _ = _cenario(qtd=2)
    # Alterar a pré-seleção depois da compra não altera o que foi comprado.
    for pi in menu.itens:
        pi.quantidade = 1
    db.session.commit()
    d = _driver(p)
    with patch('app.services.rastreio_entrega._enviar_emails_saida', return_value=0):
        iniciar_rota(d)
        iniciar_rota(d)
    with patch('app.services.email.disponivel', return_value=False):
        loja_entrega.avancar_status_entrega(p.codigo, 'entregue')
        loja_entrega.avancar_status_entrega(p.codigo, 'entregue')
    assert _saldos() == [30, 44, 46]
    assert _saldos_loja() == [50, 50, 50]
    assert SaidaProducaoSite.query.count() == 1
    assert MovEstoqueProducao.query.filter_by(tipo='saida_site_direto').count() == 3
    snap = json.loads(SaidaProducaoSite.query.one().componentes_json)
    assert [float(c['quantidade']) for c in snap] == [20, 6, 4]


@pytest.mark.parametrize('status', ['PICKED_UP', 'COMPLETED'])
def test_lalamove_coleta_ou_entrega_baixa_uma_vez(app, status):
    p, _, _, _ = _cenario()
    app.config['LALAMOVE_API_KEY'] = 'pk_test_chave'
    db.session.add(LalamoveEntrega(pedido_code=p.codigo, data_ref=hoje(),
                                  order_id='MINI-1', status='ASSIGNING_DRIVER'))
    db.session.commit()
    payload = {'apiKey': 'pk_test_chave', 'eventType': 'ORDER_STATUS_CHANGED',
               'data': {'order': {'orderId': 'MINI-1', 'status': status}}}
    with patch('app.services.email.disponivel', return_value=False):
        assert app.test_client().post('/lalamove/webhook', json=payload).status_code == 200
        assert app.test_client().post('/lalamove/webhook', json=payload).status_code == 200
    assert _saldos() == [40, 47, 48]
    assert SaidaProducaoSite.query.count() == 1


def test_falta_fica_auditada_sem_negativo_nem_rebaixa_em_retry(app):
    p, _, _, _ = _cenario(saldo=3)
    faltas = saida_producao_site.registrar(p, 'teste')
    db.session.commit()
    assert _saldos() == [0, 0, 1]
    assert [float(f['faltou']) for f in faltas] == [7]
    assert MovEstoqueProducao.query.filter_by(tipo='saida_site_direto_sem_estoque').one().quantidade == 7
    for e in EstoqueProducao.query.all():
        e.quantidade = 50
    db.session.commit()
    saida_producao_site.registrar(p, 'retry')
    db.session.commit()
    assert _saldos() == [50, 50, 50]


def test_falha_de_gravacao_nao_confirma_saida_nem_saldo(app):
    from sqlalchemy import event

    p, _, _, _ = _cenario()
    d = _driver(p)

    def falhar(*_):
        raise RuntimeError('falha no registro da saída')

    event.listen(SaidaProducaoSite, 'before_insert', falhar)
    try:
        with pytest.raises(RuntimeError, match='falha no registro'):
            iniciar_rota(d)
    finally:
        event.remove(SaidaProducaoSite, 'before_insert', falhar)
        db.session.rollback()
    assert _saldos() == [50, 50, 50]
    assert RotaInicio.query.count() == 0
    assert SaidaProducaoSite.query.count() == 0
    assert MovEstoqueProducao.query.count() == 0


def test_acerto_manual_anterior_impede_segunda_baixa(app):
    p, _, _, _ = _cenario()
    acerto_despacho.acertar(hoje(), executar=True)
    assert _saldos() == [40, 47, 48]
    saida_producao_site.registrar(p, 'coleta')
    db.session.commit()
    assert _saldos() == [40, 47, 48]
    assert SaidaProducaoSite.query.count() == 0


def _adicionar_comum(p, mini):
    it = PedidoOnlineItem(pedido_id=p.id, kind='receita', receita_id=mini.id,
                          nome=mini.nome, quantidade=2, preco_unitario=10, subtotal=20)
    p.itens.append(it)
    from app.services.baixa_venda import aplicar_venda
    aplicar_venda(p.loja_retirada_id, receita_id=mini.id, qtd=2, canal='site',
                  referencia=f'Site #{p.codigo}', pedido_ref=f'site:{p.codigo}')
    db.session.commit()


def test_acerto_de_pedido_misto_so_baixa_item_comum_restante(app):
    p, _, _, minis = _cenario()
    _adicionar_comum(p, minis[0])
    saida_producao_site.registrar(p, 'coleta')
    db.session.commit()
    assert _saldos() == [40, 47, 48]
    assert _saldos_loja() == [48, 50, 50]
    acerto_despacho.acertar(hoje(), executar=True)
    assert _saldos() == [38, 47, 48]
    assert _saldos_loja() == [50, 50, 50]
    acerto_despacho.acertar(hoje(), executar=True)
    assert _saldos() == [38, 47, 48]


def test_reembolso_apos_saida_nao_simula_devolucao_fisica(app):
    p, _, _, minis = _cenario(qtd=2)
    _adicionar_comum(p, minis[0])
    saida_producao_site.registrar(p, 'coleta')
    db.session.commit()
    assert p.status == 'pago'
    ok, msg = loja_pagamento.reduzir_item_pedido_pago(p, p.itens[0].id, 1)
    assert not ok and 'saiu da produção' in msg
    with patch('app.services.loja_pagamento._devolver_ao_plano_do_dia') as devolver_plano:
        loja_pagamento._marcar_estornado(p, None)
    devolver_plano.assert_not_called()
    db.session.commit()
    assert _saldos() == [30, 44, 46]
    assert _saldos_loja() == [48, 50, 50]
    assert SaidaProducaoSite.query.count() == 1


def test_demanda_firme_some_apos_saida_mesmo_com_status_pago(app):
    from app.services.previsao_producao import _demanda_firme_por_dia
    p, _, _, minis = _cenario()
    recs = {r.id: r for r in minis}
    antes, _ = _demanda_firme_por_dia(hoje(), hoje(), recs)
    assert [antes[r.id][hoje()] for r in minis] == [10, 3, 2]
    saida_producao_site.registrar(p, 'coleta')
    db.session.commit()
    depois, _ = _demanda_firme_por_dia(hoje(), hoje(), recs)
    assert [depois[r.id][hoje()] for r in minis] == [0, 0, 0]


@pytest.mark.parametrize('caso', ['entregue', 'cancelado', 'aguardando_pagamento', 'divulgacao', 'comum'])
def test_fluxos_excluidos_e_legado_entregue_nao_reprocessados(app, caso):
    p, _, _, _ = _cenario(sob=caso != 'comum')
    if caso == 'divulgacao':
        p.divulgacao = True
    elif caso != 'comum':
        p.status = caso
    db.session.commit()
    saida_producao_site.registrar(p, 'retry')
    db.session.commit()
    assert _saldos() == [50, 50, 50]
    assert SaidaProducaoSite.query.count() == 0


@pytest.mark.parametrize('caso', ['orfao', 'sem_snapshot', 'marcador_manual_corrompido'])
def test_erro_de_composicao_ou_auditoria_bloqueia_sem_baixa_parcial(app, caso):
    p, _, _, _ = _cenario()
    if caso == 'orfao':
        p.itens[0].componentes[0].receita_id = None
    elif caso == 'sem_snapshot':
        p.itens[0].componentes.clear()
    else:
        AppConfig.set(f'acerto_despacho_{hoje().isoformat()}', '{invalido')
    db.session.commit()
    with pytest.raises(ValueError):
        saida_producao_site.registrar(p, 'coleta')
    db.session.rollback()
    assert _saldos() == [50, 50, 50]
    assert SaidaProducaoSite.query.count() == 0


@pytest.mark.parametrize('novo', ['em_preparo', 'a_caminho', 'entregue'])
def test_admin_nao_despacha_pedido_nao_pago(app, admin_user, novo):
    p, _, _, _ = _cenario()
    p.status = 'aguardando_pagamento'
    db.session.commit()
    cliente = app.test_client()
    with cliente.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    resposta = cliente.post(f'/admin/loja-online/pedidos/{p.codigo}/status',
                           data={'novo_status': novo})
    assert resposta.status_code == 302
    db.session.refresh(p)
    assert p.status == 'aguardando_pagamento'
    assert _saldos() == [50, 50, 50]


@pytest.mark.parametrize('novo', ['a_caminho', 'entregue'])
def test_admin_confirma_saida_e_baixa_juntos(app, admin_user, novo):
    p, _, _, _ = _cenario()
    cliente = app.test_client()
    with cliente.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    with patch('app.services.email.disponivel', return_value=False):
        resposta = cliente.post(f'/admin/loja-online/pedidos/{p.codigo}/status',
                               data={'novo_status': novo})
    assert resposta.status_code == 302
    db.session.refresh(p)
    assert p.status == novo
    assert _saldos() == [40, 47, 48]


def test_saida_rele_quantidade_e_saldo_desatualizados_na_sessao(app):
    from sqlalchemy import text
    p, _, _, _ = _cenario(qtd=2)
    item = p.itens[0]
    estoque = EstoqueProducao.query.order_by(EstoqueProducao.receita_id).first()
    # Simula valores relidos depois de outra operação completar enquanto
    # este request aguardava a trava, sem atualizar a identity map local.
    db.session.execute(text('UPDATE pedido_online_item SET quantidade=1 WHERE id=:id'), {'id': item.id})
    db.session.execute(text('UPDATE estoque_producao SET quantidade=35 WHERE id=:id'), {'id': estoque.id})
    assert item.quantidade == 2 and estoque.quantidade == 50
    saida_producao_site.registrar(p, 'coleta')
    db.session.commit()
    assert _saldos() == [25, 47, 48]


def test_nova_confirmacao_de_rota_processa_nova_atribuicao(app):
    p, loja, menu, _ = _cenario()
    d = _driver(p)
    with patch('app.services.rastreio_entrega._enviar_emails_saida', return_value=0):
        iniciar_rota(d)
        a, b, c = _pis(menu)
        p2 = _pedido_com_menu(db, menu, {a: 5, b: 5, c: 5}, loja=loja, codigo='OUTRAMINI')
        p2.status, p2.data_entrega = 'pago', hoje()
        db.session.add(AtribuicaoEntrega(driver_id=d.id, pedido_code=p2.codigo,
                                        data_entrega=hoje(), ordem=2))
        db.session.commit()
        iniciar_rota(d)
    assert _saldos() == [35, 42, 43]
    assert SaidaProducaoSite.query.count() == 2
    assert RotaInicio.query.count() == 1


def test_falha_no_inicio_automatico_nao_vaza_baixa_para_commit_seguinte(app):
    from app.blueprints.driver.routes import _auto_iniciar_rota
    p, loja, menu, _ = _cenario()
    d = _driver(p)
    a, b, c = _pis(menu)
    p2 = _pedido_com_menu(db, menu, {a: 5, b: 5, c: 5}, loja=loja, codigo='INVALIDO')
    p2.status, p2.data_entrega = 'pago', hoje()
    p2.itens[0].componentes[0].receita_id = None
    db.session.add(AtribuicaoEntrega(driver_id=d.id, pedido_code=p2.codigo,
                                    data_entrega=hoje(), ordem=2))
    db.session.commit()
    atrib = AtribuicaoEntrega.query.filter_by(pedido_code=p.codigo).one()
    _auto_iniciar_rota(d, atrib)
    # Outra operação logo depois não pode efetivar parte de uma rota que falhou.
    AppConfig.set('operacao_seguinte', 'ok')
    db.session.commit()
    assert _saldos() == [50, 50, 50]
    assert SaidaProducaoSite.query.count() == 0
    assert RotaInicio.query.count() == 0


def test_cesta_fixa_baixa_receita_e_materia_prima(app):
    from test_acerto_despacho import _setup

    from app.models import MovimentacaoEstoque
    _, _, mp, cesta, p = _setup(baixar=False)
    cesta.sob_encomenda = True
    db.session.commit()
    saida_producao_site.registrar(p, 'retirada')
    db.session.commit()
    assert _saldos() == [96]  # 2 cestas x 2 croissants
    assert _saldos_loja() == [50]
    assert mp.estoque_atual == 4800  # 2 cestas x 100 g
    assert MovimentacaoEstoque.query.filter_by(tipo='saida').one().quantidade == 200
    assert SaidaProducaoSite.query.count() == 1

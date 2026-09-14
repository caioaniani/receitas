"""Painel operacional distingue cobrança do ciclo e valores de cada entrega."""
from datetime import timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import CompraKit, EntregaKit, KitCafe, PedidoOnline, ReembolsoKit
from app.utils import agora, hoje


@pytest.fixture
def grupo(app, owner_user):
    kit = KitCafe(nome='Café semanal', usuario_id=owner_user.id)
    db.session.add(kit)
    pedidos = []
    for n in (1, 2):
        pedido = PedidoOnline(nome_cliente='Cliente Teste', email_cliente='teste@example.com',
                              modo_entrega='agendada', data_entrega=hoje() + timedelta(days=7*n),
                              janela_entrega='09:00–10:00', subtotal=Decimal('40.00'),
                              frete_valor=Decimal('12.50'), valor_total=Decimal('52.50'))
        db.session.add(pedido)
        pedidos.append(pedido)
    db.session.flush()
    compra = CompraKit(kit_id=kit.id, kit_nome=kit.nome, pedido_principal_id=pedidos[0].id,
                       subtotal=Decimal('80.00'), frete_total=Decimal('25.00'),
                       valor_total=Decimal('105.00'), expira_em=agora()+timedelta(minutes=35),
                       checkout_token='b'*64)
    db.session.add(compra)
    db.session.flush()
    for n, pedido in enumerate(pedidos, 1):
        db.session.add(EntregaKit(compra_id=compra.id, pedido_id=pedido.id, ordem=n))
    db.session.commit()
    return compra, pedidos


def _cliente(app, user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    return client


def test_owner_ve_valor_do_ciclo_para_pagamento_e_agenda(app, owner_user, grupo):
    compra, pedidos = grupo
    response = _cliente(app, owner_user).get('/admin/loja-online/pedidos/'+pedidos[1].codigo)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'value="105,00"' in html
    assert 'incluindo todas as datas' in html
    assert all(p.codigo in html for p in pedidos)
    assert 'Cancelar todas as datas da compra' in html


def test_cancelar_pendente_cancela_compra_inteira(app, owner_user, grupo):
    compra, pedidos = grupo
    response = _cliente(app, owner_user).post('/admin/loja-online/pedidos/'+pedidos[1].codigo+'/cancelar')
    assert response.status_code == 302
    assert all(p.status == 'cancelado' and p.motivo_cancelamento == 'cancelado_admin' for p in pedidos)
    assert compra.pago_em is None


def test_pago_reembolsa_somente_entrega_selecionada(app, owner_user, grupo, monkeypatch):
    from app.services import loja_pagamento
    compra, pedidos = grupo
    compra.pago_em = agora()
    for p in pedidos:
        p.status, p.pago_em = 'pago', agora()
    db.session.commit()
    alvos = []
    monkeypatch.setattr(loja_pagamento, 'reembolsar_pedido', lambda p: (alvos.append(p.id) or True, 'Teste'))
    client = _cliente(app, owner_user)
    html = client.get('/admin/loja-online/pedidos/'+pedidos[1].codigo).get_data(as_text=True)
    assert 'Reembolsar e cancelar esta entrega' in html
    assert '52,50 desta entrega' in html
    client.post('/admin/loja-online/pedidos/'+pedidos[1].codigo+'/cancelar')
    assert alvos == [pedidos[1].id]


def test_reembolso_incerto_e_capacidade_aparecem_no_painel(app, owner_user, grupo):
    compra, pedidos = grupo
    compra.entregas[1].alerta_capacidade = 'Capacidade excedida: conferir produção.'
    db.session.add(ReembolsoKit(pedido_id=pedidos[1].id, valor=Decimal('52.50'),
                                pagarme_charge_id='ch_teste', status='solicitado', erro='Resposta incerta'))
    db.session.commit()
    html = _cliente(app, owner_user).get('/admin/loja-online/pedidos/'+pedidos[1].codigo).get_data(as_text=True)
    assert 'Capacidade excedida' in html and 'Resposta incerta' in html
    assert 'bloqueia nova solicitação e coleta' in html


def test_editor_nao_desloca_reserva_do_kit_sem_reagendamento(app, owner_user, grupo):
    _, pedidos = grupo
    pedido = pedidos[0]
    data_original = pedido.data_entrega
    response = _cliente(app, owner_user).post('/admin/loja-online/pedidos/'+pedido.codigo+'/editar', data={
        'nome_cliente': pedido.nome_cliente, 'email_cliente': pedido.email_cliente,
        'modo_entrega': 'agendada', 'data_entrega': (data_original+timedelta(days=1)).isoformat(),
        'janela_entrega': pedido.janela_entrega})
    assert response.status_code == 302
    db.session.refresh(pedido)
    assert pedido.data_entrega == data_original


def test_gerente_nao_cancela_compra(app, admin_user, grupo):
    _, pedidos = grupo
    response = _cliente(app, admin_user).post('/admin/loja-online/pedidos/'+pedidos[0].codigo+'/cancelar')
    assert response.status_code == 403
    assert all(p.status == 'aguardando_pagamento' for p in pedidos)

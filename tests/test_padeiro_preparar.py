"""Painel de pré-preparo da TV do padeiro: itens [BACKUP]/[ASSADO] dos pedidos
do DIA SEGUINTE, agregados por item+estado."""
from datetime import timedelta

import pytest


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente):
    return cliente.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def test_preparar_so_backup_assado_do_dia_seguinte(app, admin_user, loja, catalogo, cliente):
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    from app.utils import hoje
    rid = catalogo['receita'].id
    amanha = hoje() + timedelta(days=1)
    p1 = PedidoLoja(loja_id=loja.id, data_entrega=amanha, status='confirmado',
                    criado_por=admin_user.id)
    p2 = PedidoLoja(loja_id=loja.id, data_entrega=amanha, status='confirmado',
                    criado_por=admin_user.id)
    db.session.add_all([p1, p2])
    db.session.commit()
    db.session.add(PedidoItem(pedido_id=p1.id, receita_id=rid, quantidade=10, estado='backup'))
    db.session.add(PedidoItem(pedido_id=p1.id, receita_id=rid, quantidade=20, estado=None))
    db.session.add(PedidoItem(pedido_id=p2.id, receita_id=rid, quantidade=3, estado='backup'))
    db.session.commit()

    _login(cliente)
    j = cliente.get(f'/padeiro/preparar.json?data={hoje().isoformat()}').get_json()
    assert j['dia'] == amanha.strftime('%d/%m')
    # so o BACKUP agregado (10+3=13); o item sem estado (20) fica de fora
    assert len(j['itens']) == 1
    assert j['itens'][0]['estado_label'] == 'BACKUP'
    assert j['itens'][0]['qtd'] == 13


def test_preparar_vazio_sem_backup_assado(app, admin_user, loja, catalogo, cliente):
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    from app.utils import hoje
    p = PedidoLoja(loja_id=loja.id, data_entrega=hoje() + timedelta(days=1),
                   status='confirmado', criado_por=admin_user.id)
    db.session.add(p)
    db.session.commit()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=catalogo['receita'].id,
                              quantidade=5, estado=None))
    db.session.commit()
    _login(cliente)
    j = cliente.get(f'/padeiro/preparar.json?data={hoje().isoformat()}').get_json()
    assert j['itens'] == []


def test_card_padeiro_mostra_estado(app, admin_user, loja, catalogo, cliente):
    """O card da tela do padeiro deve exibir a tag de estado ([ASSADO]/[BACKUP])."""
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    from app.utils import hoje
    p = PedidoLoja(loja_id=loja.id, data_entrega=hoje(), status='confirmado',
                   criado_por=admin_user.id)
    db.session.add(p)
    db.session.commit()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=catalogo['receita'].id,
                              quantidade=2, estado='assado'))
    db.session.commit()
    _login(cliente)
    r = cliente.get('/padeiro/')
    assert b'[ASSADO]' in r.data


# ── estado_padrao da Receita como fallback do PedidoItem.estado ──────────

def test_preparar_usa_estado_padrao_da_receita(app, admin_user, loja, catalogo, cliente):
    """Brioche com `estado_padrao='assado'` + PedidoItem.estado=None: aparece."""
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    from app.utils import hoje
    catalogo['receita'].estado_padrao = 'assado'
    p = PedidoLoja(loja_id=loja.id, data_entrega=hoje() + timedelta(days=1),
                   status='confirmado', criado_por=admin_user.id)
    db.session.add(p)
    db.session.commit()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=catalogo['receita'].id,
                              quantidade=7, estado=None))
    db.session.commit()
    _login(cliente)
    j = cliente.get(f'/padeiro/preparar.json?data={hoje().isoformat()}').get_json()
    assert len(j['itens']) == 1
    assert j['itens'][0]['estado'] == 'assado'
    assert j['itens'][0]['qtd'] == 7


def test_pedido_item_estado_explicito_sobrescreve_estado_padrao(
        app, admin_user, loja, catalogo, cliente):
    """item.estado='backup' ganha do Receita.estado_padrao='assado'."""
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    from app.utils import hoje
    catalogo['receita'].estado_padrao = 'assado'
    p = PedidoLoja(loja_id=loja.id, data_entrega=hoje() + timedelta(days=1),
                   status='confirmado', criado_por=admin_user.id)
    db.session.add(p)
    db.session.commit()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=catalogo['receita'].id,
                              quantidade=4, estado='backup'))
    db.session.commit()
    _login(cliente)
    j = cliente.get(f'/padeiro/preparar.json?data={hoje().isoformat()}').get_json()
    assert len(j['itens']) == 1
    assert j['itens'][0]['estado'] == 'backup'


def test_pedido_item_estado_efetivo_e_label(app, admin_user, loja, catalogo):
    """`estado_efetivo` cai pra Receita.estado_padrao + label vem com tag."""
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    from app.utils import hoje
    catalogo['receita'].estado_padrao = 'assado'
    p = PedidoLoja(loja_id=loja.id, data_entrega=hoje(), status='confirmado',
                   criado_por=admin_user.id)
    db.session.add(p)
    db.session.commit()
    it = PedidoItem(pedido_id=p.id, receita_id=catalogo['receita'].id,
                    quantidade=1, estado=None)
    db.session.add(it)
    db.session.commit()
    assert it.estado_efetivo == 'assado'
    assert '[ASSADO]' in it.nome_item_com_estado

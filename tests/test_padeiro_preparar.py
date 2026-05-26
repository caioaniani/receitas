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

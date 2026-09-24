"""Junção de pedidos: ao criar um pedido para uma loja que já tem um pedido
aberto na mesma data, os itens entram no existente em vez de criar novo.

Cobre o helper (`pedido_merge`), a rota web `/pedidos/novo` e o executor do
copilot `executar_criar_pedido`.
"""
from datetime import timedelta

import pytest

pytestmark = pytest.mark.usefixtures('pedidos_antes_do_corte')


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente):
    return cliente.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def _amanha():
    from app.utils import hoje
    return hoje() + timedelta(days=1)


# ── Helper ─────────────────────────────────────────────────────────────

def test_pedido_aberto_para_merge(app, loja, admin_user, catalogo):
    from app.extensions import db
    from app.models import PedidoLoja
    from app.services.pedido_merge import pedido_aberto_para_merge

    d = _amanha()
    p = PedidoLoja(loja_id=loja.id, data_entrega=d, status='confirmado',
                   criado_por=admin_user.id)
    db.session.add(p)
    db.session.commit()

    assert pedido_aberto_para_merge(loja.id, d, 'confirmado').id == p.id
    # status diferente nao casa
    assert pedido_aberto_para_merge(loja.id, d, 'pendente') is None
    # data diferente nao casa
    assert pedido_aberto_para_merge(loja.id, d + timedelta(days=1), 'confirmado') is None
    # loja diferente nao casa
    assert pedido_aberto_para_merge(loja.id + 999, d, 'confirmado') is None
    # sem data nao mescla
    assert pedido_aberto_para_merge(loja.id, None, 'confirmado') is None
    # status nao-mesclavel (separado) nunca mescla
    p.status = 'separado'
    db.session.commit()
    assert pedido_aberto_para_merge(loja.id, d, 'separado') is None


def test_mesclar_itens_soma_e_anexa(app, loja, admin_user, catalogo):
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    from app.services.pedido_merge import mesclar_itens

    rid = catalogo['receita'].id
    p = PedidoLoja(loja_id=loja.id, data_entrega=_amanha(), status='confirmado',
                   criado_por=admin_user.id)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=rid, quantidade=10, estado=None))
    db.session.commit()

    res = mesclar_itens(p, [
        # mesma chave (receita + estado None) -> soma 10+5 = 15
        {'receita_id': rid, 'produto_id': None, 'materia_prima_id': None,
         'quantidade': 5, 'estado': None, 'observacao': None},
        # mesmo produto, estado diferente -> linha nova
        {'receita_id': rid, 'produto_id': None, 'materia_prima_id': None,
         'quantidade': 3, 'estado': 'backup', 'observacao': None},
    ], modificado_por_id=admin_user.id)
    db.session.commit()

    assert res == {'adicionados': 1, 'somados': 1}
    itens = PedidoItem.query.filter_by(pedido_id=p.id).all()
    assert len(itens) == 2
    por_estado = {i.estado: i.quantidade for i in itens}
    assert por_estado[None] == 15
    assert por_estado['backup'] == 3
    assert p.modificado_por_id == admin_user.id


# ── Rota web /pedidos/novo ─────────────────────────────────────────────

def test_web_novo_mescla_em_vez_de_duplicar(app, admin_user, loja, catalogo, cliente):
    from app.models import PedidoItem, PedidoLoja
    _login(cliente)
    rid = catalogo['receita'].id
    data = _amanha().isoformat()
    base = {'loja_id': loja.id, 'data_entrega': data, 'observacao': '',
            'item_id[]': f'r_{rid}', 'item_obs[]': '', 'item_estado[]': ''}

    cliente.post('/pedidos/novo', data={**base, 'item_qtd[]': '10'})
    cliente.post('/pedidos/novo', data={**base, 'item_qtd[]': '5'})

    pedidos = PedidoLoja.query.filter_by(loja_id=loja.id).all()
    assert len(pedidos) == 1  # nao duplicou
    item = PedidoItem.query.filter_by(pedido_id=pedidos[0].id, receita_id=rid).one()
    assert item.quantidade == 15  # 10 + 5


# ── Executor do copilot ────────────────────────────────────────────────

def test_executar_criar_pedido_mescla(app, admin_user, loja, catalogo):
    from app.models import PedidoLoja
    from app.services.copilot import executar_criar_pedido

    rid = catalogo['receita'].id
    data = _amanha().isoformat()

    def _params(qtd):
        return {'loja_id': loja.id, 'data_entrega': data, 'itens': [
            {'quantidade': qtd, 'nome_original': 'Croissant',
             'resolvido': {'tipo': 'receita', 'id': rid}}]}

    r1 = executar_criar_pedido(_params(10), admin_user)
    assert r1['ok'] and not r1.get('mesclado')
    r2 = executar_criar_pedido(_params(5), admin_user)
    assert r2['ok'] and r2.get('mesclado') is True
    assert r2['pedido_id'] == r1['pedido_id']
    assert PedidoLoja.query.filter_by(loja_id=loja.id).count() == 1


# ── Consolidacao retroativa (pedidos que ja existiam) ──────────────────

def _seed_dups(loja, admin_user, rid, qtds, d):
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    ids = []
    for q in qtds:
        p = PedidoLoja(loja_id=loja.id, data_entrega=d, status='confirmado',
                       criado_por=admin_user.id)
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=rid, quantidade=q))
        ids.append(p.id)
    db.session.commit()
    return ids


def test_consolidar_loja_data(app, loja, admin_user, catalogo):
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    from app.services.pedido_merge import consolidar_loja_data

    rid = catalogo['receita'].id
    d = _amanha()
    ids = _seed_dups(loja, admin_user, rid, (10, 5, 3), d)

    alvo, absorvidos = consolidar_loja_data(loja.id, d, 'confirmado', admin_user.id)
    db.session.commit()

    assert alvo.id == ids[0] and absorvidos == 2
    assert PedidoLoja.query.filter_by(loja_id=loja.id, status='confirmado').count() == 1
    assert PedidoLoja.query.filter_by(loja_id=loja.id, status='cancelado').count() == 2
    item = PedidoItem.query.filter_by(pedido_id=alvo.id, receita_id=rid).one()
    assert item.quantidade == 18  # 10 + 5 + 3


def test_juntar_repetidos_route(app, admin_user, loja, catalogo, cliente):
    from app.models import PedidoLoja
    rid = catalogo['receita'].id
    d = _amanha()
    _seed_dups(loja, admin_user, rid, (10, 5), d)

    _login(cliente)
    cliente.post('/padeiro/juntar-repetidos', data={'data': d.isoformat()})

    assert PedidoLoja.query.filter_by(loja_id=loja.id, status='confirmado').count() == 1
    assert PedidoLoja.query.filter_by(loja_id=loja.id, status='cancelado').count() == 1


def test_juntar_repetidos_trava_todas_lojas_antes_de_consolidar(
        app, admin_user, loja, catalogo, cliente, monkeypatch):
    from app.extensions import db
    from app.models import Loja, PedidoItem, PedidoLoja
    from app.services import estoque_helpers, pedido_merge

    outra = Loja(nome='Outra loja', ativa=True)
    db.session.add(outra)
    db.session.commit()
    ids_lojas = sorted([loja.id, outra.id])
    rid, d = catalogo['receita'].id, _amanha()
    for lj in (outra, loja):
        _seed_dups(lj, admin_user, rid, (10, 5), d)
    eventos = []
    monkeypatch.setattr(estoque_helpers, 'serializar_loja',
                        lambda lid: eventos.append(('lock', lid)))
    original = pedido_merge.consolidar_loja_data

    def consolidar(*args, **kwargs):
        # O helper faz a releitura dos pedidos. Nenhuma loja pode chegar aqui
        # sem que a transação tenha adquirido antes o conjunto inteiro.
        assert eventos[:2] == [('lock', lid) for lid in ids_lojas]
        eventos.append(('consolidar', args[0]))
        return original(*args, **kwargs)

    monkeypatch.setattr(pedido_merge, 'consolidar_loja_data', consolidar)
    _login(cliente)
    resposta = cliente.post('/padeiro/juntar-repetidos', data={'data': d.isoformat()})
    assert resposta.status_code == 302
    assert {e[1] for e in eventos if e[0] == 'consolidar'} == set(ids_lojas)
    for lid in ids_lojas:
        pedido = PedidoLoja.query.filter_by(loja_id=lid, status='confirmado').one()
        assert PedidoItem.query.filter_by(pedido_id=pedido.id).one().quantidade == 15


def test_preview_merge_de_varias_lojas_nao_adquire_travas(
        app, admin_user, loja, catalogo, monkeypatch):
    from app.extensions import db
    from app.models import Loja
    from app.services import estoque_helpers
    from app.services.copilot import _enriquecer_criar_pedido

    outra = Loja(nome='Outra loja', ativa=True)
    db.session.add(outra)
    db.session.commit()
    d = _amanha()
    pedidos = {lj.id: _seed_dups(lj, admin_user, catalogo['receita'].id, (10,), d)[0]
               for lj in (outra, loja)}

    def nao_travar(_lid):
        pytest.fail('Um preview não deve manter travas entre lojas.')

    monkeypatch.setattr(estoque_helpers, 'serializar_loja', nao_travar)
    for lid in sorted(pedidos, reverse=True):
        preview = _enriquecer_criar_pedido({'loja_id': lid, 'data_entrega': d.isoformat()})
        assert preview['merge_pedido_id'] == pedidos[lid]

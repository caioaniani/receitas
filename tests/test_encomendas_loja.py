"""Encomenda do site na TV do padeiro: 2 dias antes, véspera e, no dia, junto
com o pedido da loja de onde o site baixa (dono, 12/10/2026)."""
from datetime import timedelta

import pytest

from app.extensions import db
from app.utils import hoje


@pytest.fixture(autouse=True)
def _segunda(congela_hoje):
    congela_hoje()


def _loja(nome):
    from app.models import Loja
    lj = Loja(nome=nome, ativa=True, endereco='Rua X, 1')
    db.session.add(lj)
    db.session.commit()
    return lj


def _origem():
    from app.models import AppConfig
    lj = _loja('Loja Anesio Pinto Rosa')
    AppConfig.set('loja_site_estoque_id', lj.id)
    db.session.commit()
    return lj


def _receita(nome='Mini Brioche Enc'):
    from app.models import Receita
    r = Receita(nome=nome, categoria='Pães', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0, preco_site=8.0,
                sob_encomenda=True)
    db.session.add(r)
    db.session.commit()
    return r


def _encomenda(receita, dias, modo='agendada', loja_retirada=None, cod=None):
    from app.models import PedidoOnline, PedidoOnlineItem
    p = PedidoOnline(codigo=cod or f'E{receita.id}D{dias}{modo[:2]}',
                     nome_cliente='Cliente', email_cliente='c@x.com',
                     modo_entrega=modo, status='pago',
                     loja_retirada_id=loja_retirada.id if loja_retirada else None,
                     data_entrega=hoje() + timedelta(days=dias))
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoOnlineItem(pedido_id=p.id, kind='receita',
                                    receita_id=receita.id, nome=receita.nome,
                                    preco_unitario=8, quantidade=3, subtotal=24))
    db.session.commit()
    return p


def _pedido_loja(loja, status='confirmado', dias=0):
    from app.models import PedidoItem, PedidoLoja
    r = _receita(nome=f'Pao loja {loja.id}{status}')
    p = PedidoLoja(loja_id=loja.id, status=status,
                   data_entrega=hoje() + timedelta(days=dias))
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=r.id, quantidade=10))
    db.session.commit()
    return p


def _tv():
    from app.blueprints.padeiro.routes import _dados_listas
    return _dados_listas(hoje(), eh_hoje=True)


def _online_ids(dados):
    return [c['id'] for c in dados['a_separar'] if c['tipo'] == 'online']


def test_janela_de_dois_dias_antes(app):
    _origem()
    r = _receita()
    d1, d2 = _encomenda(r, 1), _encomenda(r, 2)
    _encomenda(r, 3)
    dados = _tv()
    assert sorted(_online_ids(dados)) == sorted([d1.id, d2.id])
    card = [c for c in dados['a_separar'] if c['tipo'] == 'online'][0]
    assert card['vai_com'] == 'Loja Anesio Pinto Rosa'


def test_no_dia_vai_dentro_do_pedido_da_anesio(app):
    anesio = _origem()
    outra = _loja('Loja Nebraska')
    r = _receita()
    enc = _encomenda(r, 0)
    pl = _pedido_loja(anesio)
    _pedido_loja(outra)
    dados = _tv()
    assert enc.id not in _online_ids(dados)
    cartao = [c for c in dados['a_separar'] if c['tipo'] == 'loja' and c['id'] == pl.id][0]
    assert [e['id'] for e in cartao['encomendas']] == [enc.id]
    outros = [c for c in dados['a_separar'] if c['tipo'] == 'loja' and c['id'] != pl.id]
    assert all(not c['encomendas'] for c in outros)


def test_retirada_vai_com_a_loja_escolhida(app):
    _origem()
    ribeiro = _loja('Loja Ribeiro do Vale')
    r = _receita()
    enc = _encomenda(r, 0, modo='retirada', loja_retirada=ribeiro)
    pl = _pedido_loja(ribeiro, status='separado')
    dados = _tv()
    cartao = [c for c in dados['aguardando'] if c['id'] == pl.id][0]
    assert [e['id'] for e in cartao['encomendas']] == [enc.id]


def test_sem_pedido_da_loja_no_dia_fica_sozinha_com_aviso(app):
    _origem()
    r = _receita()
    enc = _encomenda(r, 0)
    dados = _tv()
    card = [c for c in dados['a_separar'] if c['tipo'] == 'online'][0]
    assert card['id'] == enc.id and card['sem_pedido_loja']


def test_saida_do_pedido_da_loja_leva_a_encomenda(app, admin_user):
    from app.blueprints.pedidos.routes import _executar_envio_pedido
    from app.models import EstoqueProducao, SaidaProducaoSite
    from app.services.estoque_congelados import entrada_producao
    anesio = _origem()
    r = _receita()
    entrada_producao(receita_id=r.id, quantidade=5, usuario_id=admin_user.id)
    db.session.commit()
    enc = _encomenda(r, 0, cod='ENCJUNTO')
    pl = _pedido_loja(anesio, status='separado')
    with app.test_request_context():
        ok, msg = _executar_envio_pedido(pl, admin_user)
    from flask import g
    g.pop('_login_user', None)
    assert ok and 'ENCJUNTO' in msg
    assert SaidaProducaoSite.query.filter_by(pedido_id=enc.id).count() == 1
    ep = EstoqueProducao.query.filter_by(receita_id=r.id).first()
    assert ep.quantidade == 2  # 5 - 3 da encomenda
    # Some da TV: o pedido da loja saiu e o item da encomenda também.
    dados = _tv()
    assert enc.id not in _online_ids(dados)


def test_pedido_da_loja_ja_saiu_sem_a_encomenda_avisa(app):
    anesio = _origem()
    r = _receita()
    enc = _encomenda(r, 0)
    _pedido_loja(anesio, status='em_transporte')
    dados = _tv()
    card = [c for c in dados['a_separar'] if c['tipo'] == 'online'][0]
    assert card['id'] == enc.id and 'já saiu' in card['sem_pedido_loja']


def test_dois_pedidos_da_loja_encomenda_vai_no_de_menor_id(app, admin_user):
    from app.blueprints.pedidos.routes import _executar_envio_pedido
    from app.models import SaidaProducaoSite
    anesio = _origem()
    r = _receita()
    enc = _encomenda(r, 0)
    a = _pedido_loja(anesio, status='separado')
    b = _pedido_loja(anesio, status='separado')
    dados = _tv()
    cartoes = {c['id']: c for c in dados['aguardando']}
    assert [e['id'] for e in cartoes[a.id]['encomendas']] == [enc.id]
    assert cartoes[b.id]['encomendas'] == []
    # O #B sai primeiro: a encomenda NÃO vai com ele.
    with app.test_request_context():
        ok, msg = _executar_envio_pedido(b, admin_user)
    from flask import g
    g.pop('_login_user', None)
    assert ok and enc.codigo not in msg
    assert SaidaProducaoSite.query.filter_by(pedido_id=enc.id).count() == 0


def test_voltar_status_devolve_a_encomenda(app, admin_user):
    from app.blueprints.pedidos.routes import _aplicar_voltar_status, _executar_envio_pedido
    from app.models import EstoqueProducao, SaidaProducaoSite
    from app.services.estoque_congelados import entrada_producao
    anesio = _origem()
    r = _receita()
    entrada_producao(receita_id=r.id, quantidade=5, usuario_id=admin_user.id)
    db.session.commit()
    enc = _encomenda(r, 0, cod='ENCVOLTA')
    pl = _pedido_loja(anesio, status='separado')
    with app.test_request_context():
        _executar_envio_pedido(pl, admin_user)
    from flask import g
    g.pop('_login_user', None)
    assert EstoqueProducao.query.filter_by(receita_id=r.id).first().quantidade == 2
    _aplicar_voltar_status(pl, admin_user.id)
    db.session.commit()
    assert SaidaProducaoSite.query.filter_by(pedido_id=enc.id).count() == 0
    assert EstoqueProducao.query.filter_by(receita_id=r.id).first().quantidade == 5
    dados = _tv()
    cartao = [c for c in dados['aguardando'] if c['id'] == pl.id][0]
    assert [e['id'] for e in cartao['encomendas']] == [enc.id]

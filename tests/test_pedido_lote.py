"""Restrição de LOTE pra item de pedido medido em g/ml (dono 18/08/2026).

Caso "granola/iogurte em POTES": item a granel (g/ml) com `lote_pedido`
definido só aceita quantidade MÚLTIPLA do lote (iogurte 3000, granola
5000). Receita em unidades com lote_pedido (croissant 50) segue livre —
lá o lote só arredonda sugestão. Defesa em profundidade: web novo/editar
+ executores do copilot.
"""
from datetime import timedelta

from app.extensions import db
from app.models import PedidoItem, PedidoLoja, Receita
from app.services.pedido_lote import violacoes_de_lote, violacoes_por_ids
from app.utils import hoje


def _granel(nome='Produção - Iogurte Teste 1000ml', lote=3000):
    r = Receita(nome=nome, categoria='Produção', rendimento_qtd=1170,
                rendimento_unidade='ml', peso_base=1170.0,
                peso_unitario=1.0, lote_pedido=lote)
    db.session.add(r)
    db.session.commit()
    return r


def _unidade(nome='Croissant Teste', lote=50):
    r = Receita(nome=nome, categoria='Viennoiserie', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=90.0,
                peso_unitario=90.0, lote_pedido=lote)
    db.session.add(r)
    db.session.commit()
    return r


def _login(client, user):
    uid = user if isinstance(user, int) else user.id
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True


def test_multiplo_passa_e_quebrado_recusa(app):
    with app.app_context():
        r = _granel()
        assert violacoes_de_lote([(r, 3000)]) == []
        assert violacoes_de_lote([(r, 9000)]) == []
        erros = violacoes_de_lote([(r, 9360)])
        assert len(erros) == 1
        assert 'múltiplo de 3000' in erros[0]
        assert '9360' in erros[0]


def test_receita_em_unidades_com_lote_fica_livre(app):
    """Croissant lote 50: o lote só arredonda a sugestão — 45 na mão segue
    válido (não regredir sem ordem)."""
    with app.app_context():
        r = _unidade()
        assert violacoes_de_lote([(r, 45)]) == []


def test_granel_sem_lote_fica_livre(app):
    with app.app_context():
        r = _granel(lote=None)
        assert violacoes_de_lote([(r, 137)]) == []


def test_violacoes_por_ids_resolve_do_banco(app):
    with app.app_context():
        r = _granel()
        itens = [{'receita_id': r.id, 'quantidade': 4},
                 {'receita_id': None, 'quantidade': 3},
                 {'produto_id': 99, 'quantidade': 7}]
        erros = violacoes_por_ids(itens)
        assert len(erros) == 1 and 'múltiplo de 3000' in erros[0]


def test_post_novo_recusa_fora_do_lote(app, admin_user, loja):
    with app.app_context():
        r = _granel()
        rid, lid = r.id, loja.id
        data = hoje().isoformat()
    client = app.test_client()
    _login(client, admin_user)
    resp = client.post('/pedidos/novo', data={
        'loja_id': str(lid), 'data_entrega': data,
        'item_id[]': f'r_{rid}', 'item_qtd[]': '3',
        'item_estado[]': '', 'item_obs[]': '',
    }, follow_redirects=False)
    assert resp.status_code == 200          # re-render com flash, não cria
    with app.app_context():
        assert PedidoLoja.query.filter_by(loja_id=lid).count() == 0


def test_post_novo_aceita_multiplo(app, admin_user, loja):
    with app.app_context():
        r = _granel()
        rid, lid = r.id, loja.id
        data = hoje().isoformat()
    client = app.test_client()
    _login(client, admin_user)
    resp = client.post('/pedidos/novo', data={
        'loja_id': str(lid), 'data_entrega': data,
        'item_id[]': f'r_{rid}', 'item_qtd[]': '6000',
        'item_estado[]': '', 'item_obs[]': '',
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)
    with app.app_context():
        ped = PedidoLoja.query.filter_by(loja_id=lid).one()
        assert ped.itens[0].quantidade == 6000


def test_editar_recusa_fora_do_lote_sem_grandfather(app, admin_user, loja):
    """Decisão do dono: SEM grandfather — o 9360 antigo tem que virar
    9000/12000 ao editar."""
    with app.app_context():
        r = _granel()
        ped = PedidoLoja(loja_id=loja.id, status='pendente',
                         data_entrega=hoje() + timedelta(days=1),
                         data_pedido=hoje())
        db.session.add(ped)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=ped.id, receita_id=r.id,
                                  quantidade=9360))
        db.session.commit()
        pid, rid = ped.id, r.id
        data = (hoje() + timedelta(days=1)).isoformat()
    client = app.test_client()
    _login(client, admin_user)
    resp = client.post(f'/pedidos/{pid}/editar', data={
        'data_entrega': data, 'observacao': '',
        'item_id[]': f'r_{rid}', 'item_qtd[]': '9360',
        'item_estado[]': '', 'item_obs[]': '',
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)
    with app.app_context():
        # nada mudou — a edição foi recusada antes do REPLACE
        ped = db.session.get(PedidoLoja, pid)
        assert len(ped.itens) == 1
        assert ped.itens[0].quantidade == 9360
    # corrigindo pra múltiplo, salva
    resp2 = client.post(f'/pedidos/{pid}/editar', data={
        'data_entrega': data, 'observacao': '',
        'item_id[]': f'r_{rid}', 'item_qtd[]': '9000',
        'item_estado[]': '', 'item_obs[]': '',
    }, follow_redirects=False)
    assert resp2.status_code in (302, 303)
    with app.app_context():
        ped = db.session.get(PedidoLoja, pid)
        assert ped.itens[0].quantidade == 9000


def test_executor_criar_pedido_recusa_fora_do_lote(app, admin_user, loja):
    from app.services.copilot import executar_criar_pedido
    with app.app_context():
        r = _granel()
        params = {
            'loja_id': loja.id,
            'data_entrega': (hoje() + timedelta(days=2)).isoformat(),
            'itens': [{'nome_original': r.nome, 'quantidade': 5,
                       'resolvido': {'tipo': 'receita', 'id': r.id,
                                     'nome': r.nome}}],
        }
        res = executar_criar_pedido(params, admin_user)
        assert res['ok'] is False
        assert 'múltiplo de 3000' in res['erro']
        assert PedidoLoja.query.count() == 0


def test_executor_editar_pedido_recusa_fora_do_lote(app, admin_user, loja):
    from app.services.copilot import executar_editar_pedido
    with app.app_context():
        r = _granel()
        ped = PedidoLoja(loja_id=loja.id, status='pendente',
                         data_entrega=hoje() + timedelta(days=2),
                         data_pedido=hoje())
        db.session.add(ped)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=ped.id, receita_id=r.id,
                                  quantidade=3000))
        db.session.commit()
        res = executar_editar_pedido({
            'pedido_id': ped.id,
            'itens': [{'nome_original': r.nome, 'quantidade': 4000,
                       'resolvido': {'tipo': 'receita', 'id': r.id,
                                     'nome': r.nome}}],
        }, admin_user)
        assert res['ok'] is False
        assert 'múltiplo de 3000' in res['erro']
        ped = db.session.get(PedidoLoja, ped.id)
        assert ped.itens[0].quantidade == 3000

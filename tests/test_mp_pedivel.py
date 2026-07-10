"""Trava: MP só entra em pedido de loja se LIBERADA no checkbox "sugerir
pedido loja" do Banco de MPs (decisão do dono 07/07/2026 — "tenho itens que
as lojas estão pedindo para a indústria que não deveriam poder").

Camadas cobertas:
- typeahead do novo pedido (não oferece MP bloqueada);
- POST /pedidos/novo (server-side, POST direto/aba velha não fura);
- POST /pedidos/<id>/editar com GRANDFATHER (MP que JÁ estava no pedido
  segue válida; MP nova bloqueada é recusada) + o GET do editar ainda
  renderiza a MP antiga no select;
- copilot: resolver não oferece bloqueada (mas aceita via mp_ids_extras no
  editar) e executores recusam mesmo com params re-enviados.

Receitas e produtos seguem livres; a trava é só de MP (opt-in).
"""
from datetime import timedelta

from app.utils import hoje


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _mp(nome, liberada):
    from app.extensions import db
    from app.models import MateriaPrima
    m = MateriaPrima(nome=nome, unidade='un', custo_por_kg=10.0,
                     sugerir_pedido_loja=liberada)
    db.session.add(m)
    db.session.commit()
    return m


def _pedido_com_mp(loja, admin_user, mp):
    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    p = PedidoLoja(loja_id=loja.id, data_entrega=hoje() + timedelta(days=1),
                   status='confirmado', criado_por=admin_user.id)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, materia_prima_id=mp.id,
                              quantidade=4))
    db.session.commit()
    return p


# ── Typeahead ────────────────────────────────────────────────────────────

def test_typeahead_so_oferece_mp_liberada(app, admin_user):
    with app.app_context():
        lib = _mp('Queijo Liberado', True)
        blo = _mp('Queijo Bloqueado', False)
        lib_id, blo_id = lib.id, blo.id
    client = app.test_client()
    _login(client, admin_user)
    ids = [i['id'] for i in
           client.get('/pedidos/buscar-itens.json?q=queijo').get_json()['itens']]
    assert f'mp_{lib_id}' in ids
    assert f'mp_{blo_id}' not in ids


# ── POST /pedidos/novo ───────────────────────────────────────────────────

def test_post_novo_recusa_mp_bloqueada(app, admin_user, loja):
    from app.models import PedidoLoja
    with app.app_context():
        blo = _mp('Lagarto Cozido', False)
        blo_id, lid = blo.id, loja.id
    client = app.test_client()
    _login(client, admin_user)
    resp = client.post('/pedidos/novo', data={
        'loja_id': str(lid),
        'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
        'item_id[]': f'mp_{blo_id}',
        'item_qtd[]': '3',
        'item_estado[]': '',
        'item_obs[]': '',
    }, follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert 'não liberada' in body
    assert 'Lagarto Cozido' in body
    with app.app_context():
        assert PedidoLoja.query.filter_by(loja_id=lid).count() == 0


def test_post_novo_aceita_mp_liberada(app, admin_user, loja):
    from app.models import PedidoLoja
    with app.app_context():
        lib = _mp('Saco Pao de Queijo', True)
        lib_id, lid = lib.id, loja.id
    client = app.test_client()
    _login(client, admin_user)
    resp = client.post('/pedidos/novo', data={
        'loja_id': str(lid),
        'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
        'item_id[]': f'mp_{lib_id}',
        'item_qtd[]': '2',
        'item_estado[]': '',
        'item_obs[]': '',
    })
    assert resp.status_code in (302, 303)
    with app.app_context():
        ped = PedidoLoja.query.filter_by(loja_id=lid).one()
        assert ped.itens[0].materia_prima_id == lib_id


# ── POST /pedidos/<id>/editar (grandfather) ──────────────────────────────

def test_editar_grandfather_mantem_mp_antiga(app, admin_user, loja):
    """Pedido antigo com MP hoje bloqueada: re-enviar a mesma lista (o form
    faz REPLACE total) NÃO pode ser recusado — senão desmarcar o checkbox
    travaria a edição de pedidos legítimos."""
    from app.models import PedidoLoja
    with app.app_context():
        blo = _mp('Item Antigo Bloqueado', False)
        ped = _pedido_com_mp(loja, admin_user, blo)
        ped_id, blo_id = ped.id, blo.id
    client = app.test_client()
    _login(client, admin_user)
    resp = client.post(f'/pedidos/{ped_id}/editar', data={
        'data_entrega': (hoje() + timedelta(days=2)).isoformat(),
        'observacao': '',
        'item_id[]': f'mp_{blo_id}',
        'item_qtd[]': '6',
        'item_estado[]': '',
        'item_obs[]': '',
    })
    assert resp.status_code in (302, 303)
    with app.app_context():
        ped = PedidoLoja.query.get(ped_id)
        assert len(ped.itens) == 1
        assert ped.itens[0].materia_prima_id == blo_id
        assert ped.itens[0].quantidade == 6


def test_editar_recusa_mp_bloqueada_nova(app, admin_user, loja):
    from app.models import PedidoLoja
    with app.app_context():
        antiga = _mp('MP Antiga OK', True)
        nova_blo = _mp('MP Nova Bloqueada', False)
        ped = _pedido_com_mp(loja, admin_user, antiga)
        ped_id = ped.id
        antiga_id, nova_id = antiga.id, nova_blo.id
    client = app.test_client()
    _login(client, admin_user)
    resp = client.post(f'/pedidos/{ped_id}/editar', data={
        'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
        'observacao': '',
        'item_id[]': [f'mp_{antiga_id}', f'mp_{nova_id}'],
        'item_qtd[]': ['4', '2'],
        'item_estado[]': ['', ''],
        'item_obs[]': ['', ''],
    }, follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert 'não liberada' in body
    assert 'MP Nova Bloqueada' in body
    with app.app_context():
        ped = PedidoLoja.query.get(ped_id)
        # itens intactos — a recusa veio ANTES do REPLACE
        assert len(ped.itens) == 1
        assert ped.itens[0].materia_prima_id == antiga_id
        assert ped.itens[0].quantidade == 4


def test_editar_get_renderiza_mp_grandfathered(app, admin_user, loja):
    """A linha do item existente vem pré-preenchida com o id codificado
    mp_<id> mesmo com a MP bloqueada hoje — sem isso, o REPLACE do POST
    derrubaria o item. Com o typeahead a linha carrega o hidden value=mp_<id>
    (não depende mais de a opção existir num <select>)."""
    with app.app_context():
        blo = _mp('Grandfather no Select', False)
        ped = _pedido_com_mp(loja, admin_user, blo)
        ped_id, blo_id = ped.id, blo.id
    client = app.test_client()
    _login(client, admin_user)
    body = client.get(f'/pedidos/{ped_id}/editar').get_data(as_text=True)
    assert f'value="mp_{blo_id}"' in body
    assert 'value="Grandfather no Select"' in body


# ── Copilot ──────────────────────────────────────────────────────────────

def test_resolver_item_pedido_filtra_bloqueada(app):
    from app.services.copilot import _resolver_item_pedido
    with app.app_context():
        lib = _mp('Mussarela Liberada', True)
        blo = _mp('Mussarela Bloqueada', False)
        ms = _resolver_item_pedido('mussarela')
        ids_mp = {m['id'] for m in ms if m['tipo'] == 'mp'}
        assert lib.id in ids_mp
        assert blo.id not in ids_mp
        # exceção do editar (grandfather): com o id nos extras, resolve
        ms2 = _resolver_item_pedido('mussarela', mp_ids_extras={blo.id})
        assert blo.id in {m['id'] for m in ms2 if m['tipo'] == 'mp'}


def test_executor_criar_pedido_recusa_mp_bloqueada(app, admin_user, loja):
    """Defesa em profundidade: mesmo que um preview antigo traga MP
    bloqueada resolvida nos params, o executor recusa."""
    from app.models import PedidoLoja
    from app.services.copilot import executar_criar_pedido
    with app.app_context():
        blo = _mp('Furada de Preview', False)
        params = {
            'loja_id': loja.id,
            'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
            'itens': [{'nome_original': blo.nome, 'quantidade': 2,
                       'resolvido': {'tipo': 'mp', 'id': blo.id,
                                     'nome': blo.nome}}],
        }
        res = executar_criar_pedido(params, admin_user)
        assert res['ok'] is False
        assert 'nao liberada' in res['erro']
        assert 'Furada de Preview' in res['erro']
        assert PedidoLoja.query.count() == 0


def test_executor_editar_pedido_grandfather(app, admin_user, loja):
    """Executor do editar: MP antiga (já no pedido) passa; MP nova bloqueada
    é recusada sem tocar nos itens."""
    from app.models import PedidoLoja
    from app.services.copilot import executar_editar_pedido
    with app.app_context():
        antiga_blo = _mp('Antiga Bloqueada', False)
        nova_blo = _mp('Nova Bloqueada', False)
        ped = _pedido_com_mp(loja, admin_user, antiga_blo)
        # re-enviar a MP antiga (grandfather) → ok
        res = executar_editar_pedido({
            'pedido_id': ped.id,
            'itens': [{'nome_original': antiga_blo.nome, 'quantidade': 9,
                       'resolvido': {'tipo': 'mp', 'id': antiga_blo.id,
                                     'nome': antiga_blo.nome}}],
        }, admin_user)
        assert res['ok'] is True
        # adicionar MP nova bloqueada → recusa, itens intactos
        res2 = executar_editar_pedido({
            'pedido_id': ped.id,
            'itens': [{'nome_original': antiga_blo.nome, 'quantidade': 9,
                       'resolvido': {'tipo': 'mp', 'id': antiga_blo.id,
                                     'nome': antiga_blo.nome}},
                      {'nome_original': nova_blo.nome, 'quantidade': 1,
                       'resolvido': {'tipo': 'mp', 'id': nova_blo.id,
                                     'nome': nova_blo.nome}}],
        }, admin_user)
        assert res2['ok'] is False
        assert 'Nova Bloqueada' in res2['erro']
        ped = PedidoLoja.query.get(ped.id)
        assert len(ped.itens) == 1
        assert ped.itens[0].quantidade == 9


def test_enricher_editar_resolve_mp_grandfathered(app, admin_user, loja):
    """O enricher do editar resolve MP bloqueada que JÁ está no pedido (via
    mp_ids_extras) — sem isso o re-envio da lista derrubaria o item como
    'não resolvido'."""
    from app.services.copilot import _enriquecer_editar_pedido
    with app.app_context():
        blo = _mp('So No Pedido', False)
        ped = _pedido_com_mp(loja, admin_user, blo)
        enr = _enriquecer_editar_pedido({
            'pedido_id': ped.id,
            'itens': [{'nome': 'So No Pedido', 'quantidade': 5}],
        })
        assert enr['itens'][0]['resolvido'] is not None
        assert enr['itens'][0]['resolvido']['tipo'] == 'mp'
        assert enr['itens'][0]['resolvido']['id'] == blo.id

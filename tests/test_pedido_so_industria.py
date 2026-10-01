"""Pedido loja→indústria só do que a indústria produz (dono 01/10/2026:
"está vindo de tudo até coca cola e adicional de morango").

Regra canônica em app/services/itens_industria.py; camadas cobertas aqui:
- helper (revenda × cesta × MP liberada × inativo × recursão/ciclo);
- web: typeahead, POST /pedidos/novo, POST /pedidos/<id>/editar com
  GRANDFATHER (produto que JÁ estava no pedido segue), POST sugerir-pedido;
- copilot: resolver (com produto_ids_extras no editar), executores criar/
  editar (defesa em profundidade) e enricher do editar;
- limpeza owner-only /admin/pedidos-revenda (dry-run × executar, corte).
O motor venda+estoque e as grades estão em test_pedidos_por_venda_produtos.py
e test_reposicao_produtos_interfaces.py."""
from datetime import datetime, time, timedelta

import pytest

from app.extensions import db
from app.models import (
    MateriaPrima,
    PedidoItem,
    PedidoLoja,
    Produto,
    ProdutoItem,
    Receita,
)
from app.services.itens_industria import (
    MOTIVO_CESTA_DE_REVENDA,
    MOTIVO_INATIVO,
    MOTIVO_REVENDA,
    mensagem_produtos_nao_pediveis,
    motivo_produto_nao_fornecido,
    produto_fornecido_pela_industria,
    produtos_nao_pediveis,
)
from app.utils import hoje


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _receita(nome='Pão da Cesta', arquivada=False):
    r = Receita(nome=nome, rendimento_qtd=1, rendimento_unidade='un', peso_base=100)
    if arquivada:
        from app.utils import agora
        r.arquivada_em = agora()
    db.session.add(r)
    db.session.commit()
    return r


def _produto(nome, ativo=True):
    p = Produto(nome=nome, ativo=ativo)
    db.session.add(p)
    db.session.commit()
    return p


def _cesta(nome, *, receita=None, mp=None, produto=None, ativo=True):
    p = Produto(nome=nome, ativo=ativo)
    db.session.add(p)
    db.session.flush()
    if receita is not None:
        db.session.add(ProdutoItem(produto_id=p.id, tipo='receita', receita_id=receita.id,
                                   item_nome=receita.nome, quantidade=2))
    if mp is not None:
        db.session.add(ProdutoItem(produto_id=p.id, tipo='mp', materia_prima_id=mp.id,
                                   item_nome=mp.nome, quantidade=1))
    if produto is not None:
        db.session.add(ProdutoItem(produto_id=p.id, tipo='produto',
                                   produto_componente_id=produto.id,
                                   item_nome=produto.nome, quantidade=1))
    db.session.commit()
    return p


def _mp(nome, liberada):
    m = MateriaPrima(nome=nome, unidade='un', custo_por_kg=10.0,
                     sugerir_pedido_loja=liberada)
    db.session.add(m)
    db.session.commit()
    return m


def _pedido(loja, user, *, dias=1, status='confirmado', itens=(), criado_por=None,
            observacao=None):
    p = PedidoLoja(loja_id=loja.id, data_entrega=hoje() + timedelta(days=dias),
                   status=status, criado_por=criado_por, observacao=observacao)
    db.session.add(p)
    db.session.flush()
    for kind, obj, qtd in itens:
        db.session.add(PedidoItem(pedido_id=p.id, quantidade=qtd,
                                  receita_id=obj.id if kind == 'r' else None,
                                  produto_id=obj.id if kind == 'p' else None,
                                  materia_prima_id=obj.id if kind == 'mp' else None))
    db.session.commit()
    return p


# ── Helper ───────────────────────────────────────────────────────────────

def test_helper_revenda_cesta_mp_e_inativo(app):
    receita = _receita()
    agua = _produto('Água com gás')
    inativo = _produto('Suco antigo', ativo=False)
    mp_lib = _mp('Pão de queijo congelado', True)
    mp_blo = _mp('Detergente', False)
    cesta_receita = _cesta('Cesta de pão', receita=receita)
    cesta_mp_lib = _cesta('Cone de pão de queijo', mp=mp_lib)
    cesta_mp_blo = _cesta('Kit limpeza', mp=mp_blo)
    cesta_revenda = _cesta('Kit bebidas', produto=agua)
    cesta_de_cesta = _cesta('Caixa com cesta de pão', produto=cesta_receita)
    cesta_vazia_inativa = _cesta('Cesta inativa', receita=receita, ativo=False)

    assert motivo_produto_nao_fornecido(agua) == MOTIVO_REVENDA
    assert motivo_produto_nao_fornecido(inativo) == MOTIVO_INATIVO
    assert motivo_produto_nao_fornecido(None) == MOTIVO_INATIVO
    assert motivo_produto_nao_fornecido(cesta_vazia_inativa) == MOTIVO_INATIVO
    assert produto_fornecido_pela_industria(cesta_receita)
    assert produto_fornecido_pela_industria(cesta_mp_lib)
    assert motivo_produto_nao_fornecido(cesta_mp_blo) == MOTIVO_CESTA_DE_REVENDA
    assert motivo_produto_nao_fornecido(cesta_revenda) == MOTIVO_CESTA_DE_REVENDA
    assert produto_fornecido_pela_industria(cesta_de_cesta)


def test_helper_receita_arquivada_na_cesta_nao_conta_e_ciclo_nao_trava(app):
    morta = _receita('Receita morta', arquivada=True)
    cesta_morta = _cesta('Cesta da receita morta', receita=morta)
    assert motivo_produto_nao_fornecido(cesta_morta) == MOTIVO_CESTA_DE_REVENDA

    a = _cesta('Cesta A')
    b = _cesta('Cesta B', produto=a)
    db.session.add(ProdutoItem(produto_id=a.id, tipo='produto',
                               produto_componente_id=b.id, item_nome=b.nome,
                               quantidade=1))
    db.session.commit()
    assert motivo_produto_nao_fornecido(a) == MOTIVO_CESTA_DE_REVENDA


def test_produtos_nao_pediveis_respeita_grandfather_e_monta_mensagem(app):
    receita = _receita()
    agua = _produto('Água com gás')
    cesta = _cesta('Cesta de pão', receita=receita)
    itens = [{'produto_id': agua.id, 'quantidade': 1},
             {'produto_id': cesta.id, 'quantidade': 1},
             {'receita_id': receita.id, 'quantidade': 1},
             {'produto_id': 999999, 'quantidade': 1}]
    bloqueados = produtos_nao_pediveis(itens)
    assert [(b['nome'], b['motivo']) for b in bloqueados] == [
        ('Água com gás', MOTIVO_REVENDA), ('produto #999999', MOTIVO_INATIVO)]
    msg = mensagem_produtos_nao_pediveis(bloqueados)
    assert 'Água com gás (produto de revenda, a indústria não produz)' in msg
    assert 'não produz nem envia' in msg
    assert produtos_nao_pediveis(itens, produto_ids_extras={agua.id, 999999}) == []


# ── Web ──────────────────────────────────────────────────────────────────

def test_typeahead_so_oferece_produto_fornecido(app, admin_user):
    receita = _receita()
    agua = _produto('Água Mineral Sem Gás')
    cesta = _cesta('Água Cesta de pão', receita=receita)
    client = app.test_client()
    _login(client, admin_user)
    ids = [i['id'] for i in client.get('/pedidos/buscar-itens.json?q=agua')
           .get_json()['itens']]
    assert f'p_{cesta.id}' in ids
    assert f'p_{agua.id}' not in ids


def test_post_novo_recusa_produto_de_revenda(app, admin_user, loja, pedidos_antes_do_corte):
    agua = _produto('Água com gás')
    client = app.test_client()
    _login(client, admin_user)
    r = client.post('/pedidos/novo', data={
        'loja_id': loja.id,
        'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
        'observacao': '', 'item_id[]': f'p_{agua.id}', 'item_nome[]': agua.nome,
        'item_qtd[]': '3', 'item_obs[]': '', 'item_estado[]': '',
    })
    assert r.status_code == 200
    assert 'não produz nem envia' in r.get_data(as_text=True)
    assert PedidoLoja.query.count() == 0


def test_post_novo_aceita_cesta_montada_pela_industria(
        app, admin_user, loja, pedidos_antes_do_corte):
    receita = _receita()
    cesta = _cesta('Cesta de pão', receita=receita)
    client = app.test_client()
    _login(client, admin_user)
    r = client.post('/pedidos/novo', data={
        'loja_id': loja.id,
        'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
        'observacao': '', 'item_id[]': f'p_{cesta.id}', 'item_nome[]': cesta.nome,
        'item_qtd[]': '3', 'item_obs[]': '', 'item_estado[]': '',
    })
    assert r.status_code == 302
    assert PedidoItem.query.filter_by(produto_id=cesta.id).one().quantidade == 3


def test_editar_grandfather_mantem_revenda_antiga_e_recusa_nova(
        app, admin_user, loja, pedidos_antes_do_corte, pedido_versao_form):
    antiga = _produto('Água antiga')
    nova = _produto('Coca-Cola nova')
    ped = _pedido(loja, admin_user, itens=[('p', antiga, 4)], criado_por=admin_user.id)
    client = app.test_client()
    _login(client, admin_user)
    get = client.get(f'/pedidos/{ped.id}/editar')
    assert get.status_code == 200
    assert f'value="p_{antiga.id}"' in get.get_data(as_text=True)
    base = {'data_entrega': ped.data_entrega.isoformat(), 'observacao': ''}

    r = client.post(f'/pedidos/{ped.id}/editar', data={
        **base, 'versao_edicao': pedido_versao_form(client, ped.id),
        'item_id[]': f'p_{antiga.id}', 'item_nome[]': antiga.nome,
        'item_qtd[]': '9', 'item_obs[]': '', 'item_estado[]': ''})
    assert r.status_code in (302, 303)
    assert PedidoItem.query.filter_by(pedido_id=ped.id).one().quantidade == 9

    r2 = client.post(f'/pedidos/{ped.id}/editar', data={
        **base, 'versao_edicao': pedido_versao_form(client, ped.id),
        'item_id[]': [f'p_{antiga.id}', f'p_{nova.id}'],
        'item_nome[]': [antiga.nome, nova.nome],
        'item_qtd[]': ['9', '1'], 'item_obs[]': ['', ''], 'item_estado[]': ['', '']})
    assert r2.status_code == 400
    assert 'Coca-Cola nova' in r2.get_data(as_text=True)
    itens = PedidoItem.query.filter_by(pedido_id=ped.id).all()
    assert [(i.produto_id, i.quantidade) for i in itens] == [(antiga.id, 9)]


def test_sugerir_pedido_post_recusa_produto_de_revenda(
        app, admin_user, loja, pedidos_antes_do_corte):
    agua = _produto('Água com gás')
    client = app.test_client()
    _login(client, admin_user)
    r = client.post(f'/pedidos/lojas/{loja.id}/sugerir-pedido', data={
        'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
        'item_ref[]': f'produto:{agua.id}', 'item_qtd[]': '2'})
    assert r.status_code == 302
    assert PedidoLoja.query.count() == 0


# ── Copilot ──────────────────────────────────────────────────────────────

def test_resolver_item_pedido_filtra_revenda_com_excecao_do_editar(app):
    from app.services.copilot import _resolver_item_pedido
    receita = _receita()
    agua = _produto('Suco Verde Natural')
    cesta = _cesta('Suco Verde Cesta', receita=receita)
    ms = _resolver_item_pedido('suco verde')
    ids = {m['id'] for m in ms if m['tipo'] == 'produto'}
    assert cesta.id in ids
    assert agua.id not in ids
    ms2 = _resolver_item_pedido('suco verde', produto_ids_extras={agua.id})
    assert agua.id in {m['id'] for m in ms2 if m['tipo'] == 'produto'}


def test_executor_criar_pedido_recusa_revenda_mesmo_resolvida(
        app, admin_user, loja, pedidos_antes_do_corte):
    from app.services.copilot import executar_criar_pedido
    agua = _produto('Água Furada')
    res = executar_criar_pedido({
        'loja_id': loja.id,
        'data_entrega': (hoje() + timedelta(days=1)).isoformat(),
        'itens': [{'nome_original': agua.nome, 'quantidade': 2,
                   'resolvido': {'tipo': 'produto', 'id': agua.id, 'nome': agua.nome}}],
    }, admin_user)
    assert res['ok'] is False
    assert 'Água Furada' in res['erro'] and 'não produz nem envia' in res['erro']
    assert PedidoLoja.query.count() == 0


def test_executor_editar_pedido_grandfather_produto(
        app, admin_user, loja, pedidos_antes_do_corte):
    from app.services.copilot import executar_editar_pedido
    antiga = _produto('Antiga Revenda')
    nova = _produto('Nova Revenda')
    ped = _pedido(loja, admin_user, itens=[('p', antiga, 4)], criado_por=admin_user.id)
    item = {'nome_original': antiga.nome, 'quantidade': 9,
            'resolvido': {'tipo': 'produto', 'id': antiga.id, 'nome': antiga.nome}}
    res = executar_editar_pedido({'pedido_id': ped.id, 'itens': [item]}, admin_user)
    assert res['ok'] is True
    res2 = executar_editar_pedido({'pedido_id': ped.id, 'itens': [
        item, {'nome_original': nova.nome, 'quantidade': 1,
               'resolvido': {'tipo': 'produto', 'id': nova.id, 'nome': nova.nome}}]},
        admin_user)
    assert res2['ok'] is False
    assert 'Nova Revenda' in res2['erro']
    ped = db.session.get(PedidoLoja, ped.id)
    assert [(i.produto_id, i.quantidade) for i in ped.itens] == [(antiga.id, 9)]


def test_enricher_editar_resolve_revenda_que_ja_esta_no_pedido(app, admin_user, loja):
    from app.services.copilot import _enriquecer_editar_pedido
    agua = _produto('So No Pedido Agua')
    ped = _pedido(loja, admin_user, itens=[('p', agua, 4)], criado_por=admin_user.id)
    enr = _enriquecer_editar_pedido({'pedido_id': ped.id,
                                     'itens': [{'nome': 'So No Pedido Agua', 'quantidade': 5}]})
    assert enr['itens'][0]['resolvido'] == {'tipo': 'produto', 'id': agua.id,
                                            'nome': agua.nome, 'match': 'exato'}


# ── Limpeza owner-only ───────────────────────────────────────────────────

@pytest.fixture
def relogio_11h(monkeypatch):
    from app.services import pedido_corte
    monkeypatch.setattr(pedido_corte, 'agora',
                        lambda: datetime.combine(hoje(), time(11)))


def _login_owner(app, owner_user):
    client = app.test_client()
    client.post('/auth/login', data={'login': 'dono', 'senha': '123'})
    return client


def test_admin_pedidos_revenda_dry_run_lista_e_executar_remove(
        app, owner_user, loja, relogio_11h):
    receita = _receita()
    agua = _produto('Água com gás')
    cesta = _cesta('Cesta de pão', receita=receita)
    misto = _pedido(loja, owner_user, dias=2, itens=[('p', agua, 9), ('r', receita, 5),
                                                     ('p', cesta, 1)],
                    observacao='Gerado do histórico (rascunho)')
    so_agua = _pedido(loja, owner_user, dias=3, itens=[('p', agua, 2)],
                      criado_por=owner_user.id)
    separado = _pedido(loja, owner_user, dias=2, status='separado', itens=[('p', agua, 1)])
    passado = _pedido(loja, owner_user, dias=-1, itens=[('p', agua, 1)])
    hoje_ = _pedido(loja, owner_user, dias=0, itens=[('p', agua, 1)])
    client = _login_owner(app, owner_user)

    r = client.get('/admin/pedidos-revenda')
    assert r.status_code == 200
    d = r.get_json()
    assert d['dry_run'] is True
    assert [(li['pedido_id'], li['item'], li['quantidade'], li['criado_por'])
            for li in d['linhas']] == [
        (misto.id, 'Água com gás', 9, 'automático'),
        (so_agua.id, 'Água com gás', 2, 'dono teste')]
    assert d['pulados_corte'] == []
    assert PedidoItem.query.count() == 7

    r2 = client.get('/admin/pedidos-revenda?executar=1')
    d2 = r2.get_json()
    assert d2['removidos'] == 2
    assert d2['pedidos_cancelados_por_ficarem_vazios'] == [so_agua.id]
    misto = db.session.get(PedidoLoja, misto.id)
    assert sorted((i.receita_id, i.produto_id) for i in misto.itens) == sorted(
        [(receita.id, None), (None, cesta.id)])
    assert misto.status == 'pendente' or misto.status == 'confirmado'
    assert misto.modificado_por_id is None          # rascunho segue do cron
    assert db.session.get(PedidoLoja, so_agua.id).status == 'cancelado'
    for pid in (separado.id, passado.id, hoje_.id):
        assert PedidoItem.query.filter_by(pedido_id=pid).count() == 1

    # Idempotente: nada mais a remover.
    assert client.get('/admin/pedidos-revenda?executar=1').get_json()['removidos'] == 0


def test_admin_pedidos_revenda_respeita_corte_das_12h(app, owner_user, loja, monkeypatch):
    from app.services import pedido_corte
    monkeypatch.setattr(pedido_corte, 'agora',
                        lambda: datetime.combine(hoje(), time(13)))
    agua = _produto('Água com gás')
    amanha = _pedido(loja, owner_user, dias=1, itens=[('p', agua, 1)])
    depois = _pedido(loja, owner_user, dias=2, itens=[('p', agua, 1)])
    client = _login_owner(app, owner_user)
    d = client.get('/admin/pedidos-revenda?executar=1').get_json()
    assert [li['pedido_id'] for li in d['pulados_corte']] == [amanha.id]
    assert [li['pedido_id'] for li in d['linhas']] == [depois.id]
    assert PedidoItem.query.filter_by(pedido_id=amanha.id).count() == 1
    assert PedidoItem.query.filter_by(pedido_id=depois.id).count() == 0


def test_admin_pedidos_revenda_e_owner_only(app, admin_user):
    client = app.test_client()
    _login(client, admin_user)
    assert client.get('/admin/pedidos-revenda').status_code in (302, 403)

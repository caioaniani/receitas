"""Estoque minimo (piso da previsao) — indústria e loja (16/07/2026).

Pedido do dono: "colocar na ficha uma quantidade de estoque mínimo que devo
preencher e o sistema na parte de forecast/pedidos deve no mínimo considerar
o estoque mínimo para previsão". Duas pontas, independentes:

- INDÚSTRIA (freezer): `Receita.estoque_minimo_industria` (na ficha) —
  piso do `balanco_industria`. O alvo do dia = max(demanda, mínimo); o
  produzir nunca deixa o estoque efetivo abaixo do mínimo. Motor-agnóstico
  (vale pra pedidos/vendas/maior). Retorno e cap "só de sobras" mandam sobre
  o piso.
- LOJA (por loja × item): `EstoqueLoja.estoque_minimo` — piso do motor
  venda+estoque (`sugerir_pedidos_por_venda`). O alvo do dia =
  max(consumo·(1+segurança), mínimo).
"""
from datetime import datetime, time, timedelta

import pytest

from app.extensions import db
from app.models import EstoqueLoja, EstoqueProducao, Loja, MovEstoqueLoja, Receita
from app.services.previsao_producao import (
    balanco_industria,
    sugerir_pedidos_por_venda,
)
from app.utils import hoje


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao seg-sex + janela semanal tornaram o motor weekday-sensivel
    — congela numa SEGUNDA fixa (mesma fixture dos arquivos do cronograma;
    caso real 19/08/2026: test_cronograma_edit quebrou na QUARTA porque o
    indice 3 do grid caiu no sabado bloqueado)."""
    congela_hoje()



# ── helpers ────────────────────────────────────────────────────────────────
def _receita(nome='Croissant', **kw):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0, **kw)
    db.session.add(r)
    db.session.commit()
    return r


def _loja(nome='Loja A'):
    loja = Loja(nome=nome, ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


def _pedido(loja, status, data_entrega, receita, qtd):
    from app.models import PedidoItem, PedidoLoja
    p = PedidoLoja(loja_id=loja.id, status=status, data_entrega=data_entrega,
                   data_pedido=data_entrega)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=receita.id,
                              quantidade=qtd))
    db.session.commit()
    return p


def _estoque(loja, receita, qtd, minimo=None):
    el = EstoqueLoja(loja_id=loja.id, receita_id=receita.id, quantidade=qtd,
                     estoque_minimo=minimo)
    db.session.add(el)
    db.session.commit()
    return el


def _venda(el, data, qtd, tipo='venda_seru'):
    db.session.add(MovEstoqueLoja(
        estoque_loja_id=el.id, tipo=tipo, quantidade=qtd,
        data=datetime.combine(data, time(12, 0)), referencia='teste'))
    db.session.commit()


def _por_receita(res, receita_id):
    for it in res['itens']:
        if it['receita_id'] == receita_id:
            return it
    return None


def _prod(grade, loja_id, rid):
    loja = next((e for e in grade['lojas'] if e['loja_id'] == loja_id), None)
    return None if loja is None else next(
        (p for p in loja['produtos'] if p['receita_id'] == rid), None)


# ── INDÚSTRIA ──────────────────────────────────────────────────────────────
def test_industria_piso_eleva_producao(app):
    """Demanda 10 < mínimo 50, estoque 0 -> produz 50 (o piso)."""
    loja = _loja()
    r = _receita(estoque_minimo_industria=50)
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 10)

    res = balanco_industria(horizonte_dias=7, usar_cache=False)
    it = _por_receita(res, r.id)
    assert it is not None
    assert it['comprometido'] == 10
    assert it['demanda'] == 10                  # coluna mostra a demanda real
    assert it['produzir'] == 50                 # mas produz até o piso
    assert it['estoque_minimo'] == 50
    assert it['limitado_por_minimo'] is True


def test_industria_demanda_maior_ignora_piso(app):
    """Demanda 80 > mínimo 20 -> a demanda manda, piso não interfere."""
    loja = _loja()
    r = _receita(estoque_minimo_industria=20)
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 80)

    res = balanco_industria(horizonte_dias=7, usar_cache=False)
    it = _por_receita(res, r.id)
    assert it['produzir'] == 80
    assert it['limitado_por_minimo'] is False


def test_industria_estoque_cobre_o_piso(app):
    """Estoque 60 cobre o piso 50 (e a demanda 10) -> não produz nada."""
    loja = _loja()
    r = _receita(estoque_minimo_industria=50)
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=60))
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 10)

    res = balanco_industria(horizonte_dias=7, usar_cache=False)
    it = _por_receita(res, r.id)
    assert it['em_estoque'] == 60
    assert it['produzir'] == 0
    assert it['limitado_por_minimo'] is False


def test_industria_minimo_aparece_sem_demanda_nem_estoque(app):
    """Receita com piso cadastrado NUNCA some da tela: sem estoque, sem
    demanda e sem WIP ainda aparece (senão o piso nunca valeria) e sugere
    produzir o mínimo cheio."""
    _loja()
    r = _receita(estoque_minimo_industria=40)

    res = balanco_industria(horizonte_dias=7, usar_cache=False)
    it = _por_receita(res, r.id)
    assert it is not None                       # não some
    assert it['produzir'] == 40
    assert it['limitado_por_minimo'] is True


def test_industria_sem_minimo_inalterado(app):
    """Sem piso cadastrado: comportamento idêntico ao de antes."""
    loja = _loja()
    r = _receita()                              # sem estoque_minimo_industria
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 30)

    res = balanco_industria(horizonte_dias=7, usar_cache=False)
    it = _por_receita(res, r.id)
    assert it['produzir'] == 30
    assert it['estoque_minimo'] == 0
    assert it['limitado_por_minimo'] is False


def test_industria_retorno_nao_produz_nem_com_piso(app):
    """Retorno nunca é produzido — o piso não fura essa regra."""
    r_dest = _receita('Croissant Retorno', estoque_minimo_industria=50)
    r_orig = _receita('Croissant Tradicional')
    r_orig.retorno_receita_id = r_dest.id
    db.session.add(EstoqueProducao(receita_id=r_dest.id, quantidade=10))
    db.session.commit()

    res = balanco_industria(horizonte_dias=7, usar_cache=False)
    it = _por_receita(res, r_dest.id)
    assert it is not None
    assert it['retorno'] is True
    assert it['produzir'] == 0                  # nunca produz, nem com piso 50
    assert it['limitado_por_minimo'] is False


# ── LOJA ───────────────────────────────────────────────────────────────────
def test_loja_piso_repoe_ate_o_colchao(app):
    """Item sem venda mas com mínimo 50 e estoque 0: aparece e é reposto até
    o colchão no 1º dia; depois já está coberto."""
    loja = _loja()
    r = _receita('Pao')
    _estoque(loja, r, 0, minimo=50)

    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=0)
    p = _prod(grade, loja.id, r.id)
    assert p is not None                        # não some mesmo sem venda
    assert p['estoque_minimo'] == 50
    assert p['por_dia'][0] == 50
    assert p['por_dia'][1] == 0                 # colchão já coberto


def test_loja_minimo_maior_que_venda_eleva_pedido(app):
    """Vende ~2/dia, mas mínimo 30: repõe até 30, não até a venda média."""
    loja = _loja()
    r = _receita('Pao')
    el = _estoque(loja, r, 0, minimo=30)
    hoje_d = hoje()
    for sem in range(1, 7):
        for dow in range(7):
            d = hoje_d - timedelta(days=7 * sem)
            d = d - timedelta(days=d.weekday()) + timedelta(days=dow)
            if d < hoje_d:
                _venda(el, d, 2)

    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=0)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['por_dia'][0] == 30                # max(2, 30)


def test_loja_estoque_cobre_o_minimo(app):
    """Estoque 60 >= mínimo 50 (e sem venda): não pede nada."""
    loja = _loja()
    r = _receita('Pao')
    _estoque(loja, r, 60, minimo=50)

    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=0)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert sum(p['por_dia']) == 0


def test_loja_sem_minimo_inalterado(app):
    """Sem mínimo cadastrado: pede só o que a venda pede (comportamento
    antigo). Vende 10/dia, estoque 0 -> 1º dia pede 10, não um colchão."""
    loja = _loja()
    r = _receita('Pao')
    el = _estoque(loja, r, 0)                   # sem minimo
    hoje_d = hoje()
    for sem in range(1, 7):
        for dow in range(7):
            d = hoje_d - timedelta(days=7 * sem)
            d = d - timedelta(days=d.weekday()) + timedelta(days=dow)
            if d < hoje_d:
                _venda(el, d, 10)

    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=0)
    p = _prod(grade, loja.id, r.id)
    assert p is not None
    assert p['estoque_minimo'] == 0
    assert p['por_dia'][0] == 10                # só a venda do dia


# ── persistência (rotas) ───────────────────────────────────────────────────
def test_ficha_salva_estoque_minimo_industria(app, admin_user):
    with app.app_context():
        r = _receita('Ficha Min')
        rid = r.id
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.post(f'/receitas/{rid}/salvar', data={
        'nome': 'Ficha Min', 'categoria': 'Paes', 'rendimento_qtd': '1',
        'rendimento_unidade': 'un', 'peso_base': '100',
        'estoque_minimo_industria': '50',
    }, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert Receita.query.get(rid).estoque_minimo_industria == 50


def test_ficha_estoque_minimo_vazio_vira_null(app, admin_user):
    with app.app_context():
        r = _receita('Ficha Min2', estoque_minimo_industria=99)
        rid = r.id
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    client.post(f'/receitas/{rid}/salvar', data={
        'nome': 'Ficha Min2', 'categoria': 'Paes', 'rendimento_qtd': '1',
        'rendimento_unidade': 'un', 'peso_base': '100',
        'estoque_minimo_industria': '',
    }, follow_redirects=True)
    with app.app_context():
        assert Receita.query.get(rid).estoque_minimo_industria is None


def test_rota_salvar_minimos_loja(app, admin_user):
    loja = _loja('Loja Centro')
    r = _receita('Pao')
    el = _estoque(loja, r, 5)
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.post('/pedidos/estoque-loja/minimos', data={
        'loja_id': str(loja.id), 'estoque_id[]': str(el.id), 'minimo[]': '40',
    }, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert EstoqueLoja.query.get(el.id).estoque_minimo == 40


def test_rota_salvar_minimos_vazio_limpa(app, admin_user):
    loja = _loja()
    r = _receita('Pao')
    el = _estoque(loja, r, 5, minimo=20)
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    client.post('/pedidos/estoque-loja/minimos', data={
        'loja_id': str(loja.id), 'estoque_id[]': str(el.id), 'minimo[]': '',
    }, follow_redirects=True)
    with app.app_context():
        assert EstoqueLoja.query.get(el.id).estoque_minimo is None


def test_rota_salvar_minimos_ignora_item_de_outra_loja(app, admin_user):
    """O item precisa pertencer à loja do form — cross-loja é ignorado."""
    loja_a = _loja('Loja A')
    loja_b = _loja('Loja B')
    r = _receita('Pao')
    el_b = _estoque(loja_b, r, 5)
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    client.post('/pedidos/estoque-loja/minimos', data={
        'loja_id': str(loja_a.id), 'estoque_id[]': str(el_b.id),
        'minimo[]': '99',
    }, follow_redirects=True)
    with app.app_context():
        assert EstoqueLoja.query.get(el_b.id).estoque_minimo is None


def test_rota_estoque_loja_mostra_coluna_minimo(app, admin_user):
    loja = _loja('Loja Render')
    r = _receita('Pão Francês')
    _estoque(loja, r, 8, minimo=30)
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/pedidos/estoque-loja?loja=%d' % loja.id)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Mínimo' in body
    assert 'name="minimo[]"' in body
    assert 'Salvar mínimos' in body
    assert 'value="30"' in body                 # o mínimo cadastrado pré-preenchido


def test_rota_estoque_loja_separa_por_tipo(app, admin_user):
    """A tabela agrupa por tipo de item: Receitas / Produtos / Matérias-primas.
    Cada item cai só no seu grupo e a linha (estoque_id) aparece uma vez."""
    from app.models import MateriaPrima, Produto
    loja = _loja('Loja Grupos')
    r = _receita('Pão Sourdough')
    p = Produto(nome='Cesta Café', ativo=True)
    mp = MateriaPrima(nome='Farinha T1', unidade='kg', custo_por_kg=5.0)
    db.session.add_all([p, mp])
    db.session.commit()
    el_r = _estoque(loja, r, 5)
    el_p = EstoqueLoja(loja_id=loja.id, produto_id=p.id, quantidade=3)
    el_mp = EstoqueLoja(loja_id=loja.id, materia_prima_id=mp.id, quantidade=7)
    db.session.add_all([el_p, el_mp])
    db.session.commit()

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/pedidos/estoque-loja?loja=%d' % loja.id)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # os três cabeçalhos de grupo aparecem, na ordem receita > produto > MP
    # (assinatura própria do header pra não casar labels soltos na página)
    sig_rec = 'Receitas <span class="fw-normal">'
    sig_prod = 'Produtos <span class="fw-normal">'
    sig_mp = 'Matérias-primas <span class="fw-normal">'
    assert sig_rec in body and sig_prod in body and sig_mp in body
    assert body.index(sig_rec) < body.index(sig_prod) < body.index(sig_mp)
    # cada item aparece exatamente uma vez no form (uma linha, um estoque_id)
    for el in (el_r, el_p, el_mp):
        assert body.count('name="estoque_id[]" value="%d"' % el.id) == 1

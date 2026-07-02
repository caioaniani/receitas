"""Devolução de sobras loja → indústria (duas pontas) + política "só de sobras".

Caso de negócio (02/07/2026): croissants tradicionais que sobram nas lojas
voltam pra indústria e viram Croissant Almond. Cobre:
- service: baixa loja + credita indústria na receita de RETORNO (FK), atômico;
- fallback sem retorno configurado (credita a própria receita);
- saldo insuficiente na loja (baixa o que há, indústria recebe cheio, aviso);
- estorno das duas pontas (idempotente; indústria já consumida → parcial);
- rota web (tipo 'devolucao' vira duas pontas; 'sobra'/'perda' não caem mais
  em 'venda' — bug pré-existente corrigido);
- tool do copilot (executor + permissões);
- MRP: Almond capado ao estoque de retorno (balanço) e retorno nunca vira
  linha de produção com quantidade.
"""
from app.models import (
    EstoqueLoja,
    EstoqueProducao,
    Loja,
    MovEstoqueLoja,
    MovEstoqueProducao,
    Receita,
)
from app.services import devolucao


def _receita(db, nome, **kw):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=kw.pop('rend', 1),
                rendimento_unidade='un', peso_base=100.0, **kw)
    db.session.add(r)
    db.session.commit()
    return r


def _loja(db, nome='Ribeiro do Vale'):
    lj = Loja(nome=nome, ativa=True)
    db.session.add(lj)
    db.session.commit()
    return lj


def _estoque_loja(db, loja, rec, qtd):
    el = EstoqueLoja(loja_id=loja.id, receita_id=rec.id, quantidade=qtd)
    db.session.add(el)
    db.session.commit()
    return el


def _setup_croissants(db, com_retorno=True, saldo_loja=20):
    """Croissant Tradicional (+ retorno configurado) + loja com saldo."""
    trad = _receita(db, 'Croissant Tradicional')
    retorno = _receita(db, 'Croissant Tradicional — Retorno')
    if com_retorno:
        trad.retorno_receita_id = retorno.id
        db.session.commit()
    loja = _loja(db)
    el = _estoque_loja(db, loja, trad, saldo_loja)
    return trad, retorno, loja, el


def test_devolucao_duas_pontas_na_receita_de_retorno(app, admin_user):
    from app.extensions import db
    trad, retorno, loja, el = _setup_croissants(db)
    r = devolucao.devolver_industria(
        loja.id, [{'tipo': 'receita', 'id': trad.id, 'qtd': 12}], admin_user.id)

    assert r['itens'][0]['destino'] == 'Croissant Tradicional — Retorno'
    assert r['avisos'] == []
    db.session.refresh(el)
    assert el.quantidade == 8                       # 20 - 12
    ep = EstoqueProducao.query.filter_by(receita_id=retorno.id).first()
    assert ep is not None and ep.quantidade == 12   # credito no RETORNO
    # NADA creditado na receita original da industria
    assert EstoqueProducao.query.filter_by(receita_id=trad.id).first() is None
    # Movimentos das duas pontas amarrados pelo token
    token = r['token']
    ml = MovEstoqueLoja.query.filter_by(tipo='devolucao_industria').first()
    mi = MovEstoqueProducao.query.filter_by(tipo='retorno_loja').first()
    assert token in ml.referencia and token in mi.referencia


def test_devolucao_sem_retorno_configurado_credita_a_propria(app, admin_user):
    from app.extensions import db
    trad, _retorno, loja, _el = _setup_croissants(db, com_retorno=False)
    devolucao.devolver_industria(
        loja.id, [{'tipo': 'receita', 'id': trad.id, 'qtd': 5}], admin_user.id)
    ep = EstoqueProducao.query.filter_by(receita_id=trad.id).first()
    assert ep is not None and ep.quantidade == 5


def test_devolucao_saldo_insuficiente_avisa_e_credita_cheio(app, admin_user):
    """Verdade física: 12 croissants chegaram na indústria mesmo que a loja
    mostrasse só 8 — baixa 8 (a zero), credita 12, avisa a divergência."""
    from app.extensions import db
    trad, retorno, loja, el = _setup_croissants(db, saldo_loja=8)
    r = devolucao.devolver_industria(
        loja.id, [{'tipo': 'receita', 'id': trad.id, 'qtd': 12}], admin_user.id)
    assert len(r['avisos']) == 1
    db.session.refresh(el)
    assert el.quantidade == 0
    ep = EstoqueProducao.query.filter_by(receita_id=retorno.id).first()
    assert ep.quantidade == 12
    # A falta fica registrada como movimento visível
    assert MovEstoqueLoja.query.filter_by(
        tipo='devolucao_industria_sem_estoque').count() == 1


def test_devolucao_entrada_invalida(app, admin_user):
    import pytest

    from app.extensions import db
    trad, _r, loja, _el = _setup_croissants(db)
    with pytest.raises(ValueError):
        devolucao.devolver_industria(loja.id, [], admin_user.id)
    with pytest.raises(ValueError):
        devolucao.devolver_industria(
            loja.id, [{'tipo': 'receita', 'id': trad.id, 'qtd': 0}],
            admin_user.id)
    with pytest.raises(ValueError):
        devolucao.devolver_industria(
            loja.id, [{'tipo': 'receita', 'id': 99999, 'qtd': 1}],
            admin_user.id)


def test_estorno_reverte_as_duas_pontas(app, admin_user):
    from app.extensions import db
    trad, retorno, loja, el = _setup_croissants(db)
    r = devolucao.devolver_industria(
        loja.id, [{'tipo': 'receita', 'id': trad.id, 'qtd': 12}], admin_user.id)
    est = devolucao.estornar_devolucao(r['token'], admin_user.id)
    assert est['avisos'] == []
    db.session.refresh(el)
    assert el.quantidade == 20                      # loja re-creditada
    ep = EstoqueProducao.query.filter_by(receita_id=retorno.id).first()
    assert ep.quantidade == 0                       # industria re-baixada


def test_estorno_idempotente(app, admin_user):
    import pytest

    from app.extensions import db
    trad, _r, loja, _el = _setup_croissants(db)
    r = devolucao.devolver_industria(
        loja.id, [{'tipo': 'receita', 'id': trad.id, 'qtd': 3}], admin_user.id)
    devolucao.estornar_devolucao(r['token'], admin_user.id)
    with pytest.raises(ValueError):
        devolucao.estornar_devolucao(r['token'], admin_user.id)


def test_estorno_industria_ja_consumida_e_parcial(app, admin_user):
    """Indústria já rechearam parte (virou Almond): estorna o que resta e avisa."""
    from app.extensions import db
    trad, retorno, loja, el = _setup_croissants(db)
    r = devolucao.devolver_industria(
        loja.id, [{'tipo': 'receita', 'id': trad.id, 'qtd': 12}], admin_user.id)
    ep = EstoqueProducao.query.filter_by(receita_id=retorno.id).first()
    ep.quantidade = 4                               # consumiu 8 dos 12
    db.session.commit()
    est = devolucao.estornar_devolucao(r['token'], admin_user.id)
    assert len(est['avisos']) == 1
    db.session.refresh(ep)
    assert ep.quantidade == 0                       # baixou o que havia (4)
    db.session.refresh(el)
    assert el.quantidade == 20                      # loja recebe o baixado cheio


# ── Rota web ─────────────────────────────────────────────────────────────────

def _login(client, uid):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True


def test_rota_registrar_devolucao_duas_pontas(app, admin_user):
    from app.extensions import db
    trad, retorno, loja, el = _setup_croissants(db)
    c = app.test_client()
    _login(c, admin_user.id)
    resp = c.post('/pedidos/estoque-loja/registrar', data={
        'loja_id': str(loja.id),
        'estoque_id[]': [str(el.id)],
        'qtd[]': ['7'],
        'tipo[]': ['devolucao'],
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)
    db.session.refresh(el)
    assert el.quantidade == 13
    ep = EstoqueProducao.query.filter_by(receita_id=retorno.id).first()
    assert ep is not None and ep.quantidade == 7


def test_rota_registrar_perda_nao_vira_venda(app, admin_user):
    """Bug pré-existente: 'perda' não estava em TIPOS_VALIDOS e caía no
    fallback 'venda' — perda aparecia como venda manual no histórico."""
    from app.extensions import db
    trad, _r, loja, el = _setup_croissants(db)
    c = app.test_client()
    _login(c, admin_user.id)
    c.post('/pedidos/estoque-loja/registrar', data={
        'loja_id': str(loja.id),
        'estoque_id[]': [str(el.id)],
        'qtd[]': ['2'],
        'tipo[]': ['perda'],
    })
    mov = MovEstoqueLoja.query.filter_by(estoque_loja_id=el.id).first()
    assert mov.tipo == 'perda'


def test_rota_estorno_devolucao(app, admin_user):
    from app.extensions import db
    trad, retorno, loja, el = _setup_croissants(db)
    r = devolucao.devolver_industria(
        loja.id, [{'tipo': 'receita', 'id': trad.id, 'qtd': 6}], admin_user.id)
    c = app.test_client()
    _login(c, admin_user.id)
    resp = c.post('/pedidos/devolucao/estornar',
                  data={'token': r['token'], 'loja_id': str(loja.id)})
    assert resp.status_code in (302, 303)
    db.session.refresh(el)
    assert el.quantidade == 20


# ── Tool do copilot ──────────────────────────────────────────────────────────

def test_tool_devolver_industria_registrada():
    from app.services.copilot import PAPEIS_POR_TOOL, REQUER_APROVACAO, TOOLS
    nomes = {t['name'] for t in TOOLS}
    assert 'devolver_industria' in nomes
    assert 'devolver_industria' in REQUER_APROVACAO
    assert PAPEIS_POR_TOOL['devolver_industria'] == {'admin', 'gerente'}


def test_executor_copilot_devolver(app, admin_user):
    from app.extensions import db
    from app.services import copilot
    trad, retorno, loja, el = _setup_croissants(db)
    r = copilot.executar_devolver_industria({
        'loja_nome': 'ribeiro',
        'itens': [{'nome': 'Croissant Tradicional', 'quantidade': 10}],
    }, admin_user)
    assert r['ok'] is True
    assert r['total_devolvidos'] == 1
    db.session.refresh(el)
    assert el.quantidade == 10
    ep = EstoqueProducao.query.filter_by(receita_id=retorno.id).first()
    assert ep.quantidade == 10


def test_executor_copilot_sem_loja(app, admin_user):
    from app.services import copilot
    r = copilot.executar_devolver_industria(
        {'itens': [{'nome': 'X', 'quantidade': 1}]}, admin_user)
    assert r['ok'] is False


def test_enricher_devolver_mostra_destino(app, admin_user):
    from app.extensions import db
    from app.services.copilot import _enriquecer_devolver_industria
    trad, retorno, loja, _el = _setup_croissants(db)
    out = _enriquecer_devolver_industria({
        'loja_nome': loja.nome,
        'itens': [{'nome': 'Croissant Tradicional', 'quantidade': 4}],
    }, admin_user)
    assert out['loja_id'] == loja.id
    it = out['itens'][0]
    assert it['resolvido']['id'] == trad.id
    assert it['destino_industria'] == 'Croissant Tradicional — Retorno'
    assert it['estoque_atual'] == 20


# ── MRP: política "só de sobras" ─────────────────────────────────────────────

def _almond_com_retorno(db, retorno_qtd):
    """Almond consome 1:1 a receita de retorno; retorno com `retorno_qtd` no
    congelado. Retorna (almond, retorno)."""
    from app.models import ReceitaIngrediente
    retorno = _receita(db, 'Croissant Tradicional — Retorno')
    trad = _receita(db, 'Croissant Tradicional')
    trad.retorno_receita_id = retorno.id
    almond = _receita(db, 'Croissant Almond')
    db.session.add(ReceitaIngrediente(
        receita_id=almond.id, tipo='receita', sub_receita_id=retorno.id,
        ingrediente_nome=retorno.nome, porcentagem=1))
    if retorno_qtd:
        db.session.add(EstoqueProducao(receita_id=retorno.id,
                                       quantidade=retorno_qtd))
    db.session.commit()
    return almond, retorno


def test_balanco_capa_almond_ao_retorno_disponivel(app):
    """Demanda de 40 almonds mas só 15 retornados → Produzir = 15 (nunca puxa
    produção/massa fresca pra cobrir os 25 que faltam)."""
    from datetime import timedelta

    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    from app.services.previsao_producao import balanco_industria, invalidar_sugestao_cache
    almond, _retorno = _almond_com_retorno(db, retorno_qtd=15)
    loja = _loja(db)
    from app.utils import hoje
    ped = PedidoLoja(loja_id=loja.id, status='pendente',
                     data_entrega=hoje() + timedelta(days=1))
    db.session.add(ped)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=ped.id, receita_id=almond.id,
                              quantidade=40))
    db.session.commit()

    invalidar_sugestao_cache()
    bal = balanco_industria(usar_cache=False)
    item = next(i for i in bal['itens'] if i['receita_id'] == almond.id)
    assert item['comprometido'] == 40
    assert item['produzir'] == 15                   # capado às sobras
    assert item['limitado_por_retorno'] is not None
    assert item['limitado_por_retorno']['disponivel'] == 15


def test_balanco_sem_cap_quando_retorno_cobre(app):
    from datetime import timedelta

    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    from app.services.previsao_producao import balanco_industria, invalidar_sugestao_cache
    almond, _retorno = _almond_com_retorno(db, retorno_qtd=50)
    loja = _loja(db)
    from app.utils import hoje
    ped = PedidoLoja(loja_id=loja.id, status='pendente',
                     data_entrega=hoje() + timedelta(days=1))
    db.session.add(ped)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=ped.id, receita_id=almond.id,
                              quantidade=40))
    db.session.commit()

    invalidar_sugestao_cache()
    bal = balanco_industria(usar_cache=False)
    item = next(i for i in bal['itens'] if i['receita_id'] == almond.id)
    assert item['produzir'] == 40                   # sobras cobrem, sem cap
    assert item['limitado_por_retorno'] is None


def test_cronograma_nao_manda_produzir_retorno(app):
    """A linha do retorno no cronograma nunca vem com produção > 0 — retorno
    não é produzível (só entra por devolução de loja)."""
    from datetime import timedelta

    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja
    from app.services.previsao_producao import cronograma_producao, invalidar_sugestao_cache
    almond, retorno = _almond_com_retorno(db, retorno_qtd=15)
    loja = _loja(db)
    from app.utils import hoje
    ped = PedidoLoja(loja_id=loja.id, status='pendente',
                     data_entrega=hoje() + timedelta(days=1))
    db.session.add(ped)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=ped.id, receita_id=almond.id,
                              quantidade=40))
    db.session.commit()

    invalidar_sugestao_cache()
    cron = cronograma_producao()
    linhas = {rr['receita_id']: rr for rr in cron['receitas']}
    alm = linhas.get(almond.id)
    assert alm is not None
    assert alm['total'] <= 15                       # capado às sobras
    ret = linhas.get(retorno.id)
    if ret is not None:                             # linha-insumo informativa
        assert ret['total'] == 0
        assert ret.get('retorno') is True

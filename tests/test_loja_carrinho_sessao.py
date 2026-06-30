"""Carrinho na SESSÃO do servidor (fonte de verdade) — 30/06/2026.

Antes o carrinho vivia só no localStorage e SUMIA quando o navegador (Safari
iPhone) descartava o storage no meio do checkout. Agora vive na sessão: gravado
via POST /loja/api/carrinho, injetado em #carrinho-sessao em toda página, lido
da sessão no checkout. Não tem como sumir por causa do navegador.
"""
from decimal import Decimal

import pytest

pytestmark = pytest.mark.loja_host


def _catalogo(db):
    from app.models import AppConfig, EstoqueLoja, Loja, Produto
    loja = Loja(nome='Anesio', endereco='Anésio Pinto Rosa, 78', ativa=True)
    db.session.add(loja)
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', loja.id)
    box = Produto(nome='Box Mimo', categoria='Cestas',
                  preco_site=Decimal('166'), ativo=True)
    db.session.add(box)
    db.session.commit()
    db.session.add(EstoqueLoja(loja_id=loja.id, produto_id=box.id, quantidade=50))
    db.session.commit()
    return {'box': box.id}


def test_api_carrinho_grava_na_sessao(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    from app.extensions import db
    with app.app_context():
        ids = _catalogo(db)
    c = app.test_client()
    r = c.post('/loja/api/carrinho', json={'itens': [
        {'kind': 'produto', 'id': ids['box'], 'qtd': 3}]})
    assert r.status_code == 200
    assert r.get_json()['count'] == 3


def test_carrinho_persiste_entre_requisicoes(app, monkeypatch):
    """O PONTO da mudança: o carrinho NÃO depende do navegador. Gravado na
    sessão, ele aparece em QUALQUER página seguinte (carrinho e checkout) —
    não some."""
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    from app.extensions import db
    with app.app_context():
        ids = _catalogo(db)
    c = app.test_client()
    c.post('/loja/api/carrinho', json={'itens': [
        {'kind': 'produto', 'id': ids['box'], 'qtd': 1}]})
    # página do carrinho enxerga (injetado da sessão)
    assert b'Box Mimo' in c.get('/loja/carrinho').data
    # e o checkout também — fonte é a mesma sessão
    assert b'Box Mimo' in c.get('/loja/checkout').data


def test_api_carrinho_substitui_e_valida(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    from app.extensions import db
    with app.app_context():
        ids = _catalogo(db)
    c = app.test_client()
    c.post('/loja/api/carrinho', json={'itens': [
        {'kind': 'produto', 'id': ids['box'], 'qtd': 5}]})
    # substitui (não soma ao anterior) + descarta item inválido
    r = c.post('/loja/api/carrinho', json={'itens': [
        {'kind': 'produto', 'id': ids['box'], 'qtd': 2},
        {'kind': 'lixo', 'id': 'x', 'qtd': 1}]})
    j = r.get_json()
    assert j['count'] == 2
    assert len(j['itens']) == 1   # o inválido sumiu


def test_api_carrinho_cap_itens(app, monkeypatch):
    """Teto de itens pra não estourar o cookie de sessão (~4KB)."""
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    c = app.test_client()
    itens = [{'kind': 'produto', 'id': i, 'qtd': 1} for i in range(1, 201)]
    r = c.post('/loja/api/carrinho', json={'itens': itens})
    assert len(r.get_json()['itens']) == 60

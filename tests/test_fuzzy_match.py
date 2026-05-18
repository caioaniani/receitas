"""Smoke tests do fuzzy match — proximidade + apelido global.

Regressoes que cobrimos:
- 'sourdough' nao deve resolver pra 'Mini Sourdough'
- 'pain au chocolat' nao deve resolver pra 'Pain au Chocolat Bicolor'
- Apelido salvo em LojaProdutoMap vale globalmente
"""
import pytest


def test_score_proximidade_prefere_starts_with(app):
    """Score: nome que comeca com query vence."""
    from app.services.copilot import _score_proximidade
    # 'Croissant' starts_with → score (0, diff_len) menor
    s_base = _score_proximidade('croissant', 'Croissant')
    s_mini = _score_proximidade('croissant', 'Mini Croissant')
    assert s_base < s_mini


def test_score_proximidade_prefere_mais_curto(app):
    """Quando ambos starts_with, vence o mais curto."""
    from app.services.copilot import _score_proximidade
    s_curto = _score_proximidade('pain au chocolat', 'Pain au Chocolat')
    s_longo = _score_proximidade('pain au chocolat', 'Pain au Chocolat Bicolor')
    assert s_curto < s_longo


def test_resolver_produto_sourdough(app):
    """Sourdough deve vencer Mini Sourdough."""
    from app.extensions import db
    from app.services.copilot import _resolver_produto
    from tests.conftest import _make_receita
    db.session.add_all([
        _make_receita('Mini Sourdough'),
        _make_receita('Sourdough'),
    ])
    db.session.commit()
    matches = _resolver_produto('sourdough')
    assert matches, 'deveria achar algum match'
    assert matches[0]['nome'] == 'Sourdough'


def test_apelido_global_loja_lote(app, loja):
    """Apelido salvo em LojaProdutoMap vale na entrada em lote da loja."""
    from app.extensions import db
    from app.models import LojaProdutoMap
    from app.services import estoque_loja_lote as svc
    from tests.conftest import _make_receita
    from datetime import datetime
    r = _make_receita('Pao Frances Fermentado')
    db.session.add(r)
    db.session.flush()
    mp = LojaProdutoMap(nome_digitado='PFR', receita_id=r.id,
                         confirmado_em=datetime.utcnow())
    db.session.add(mp)
    db.session.commit()
    out = svc.resolver_lista(
        [{'linha': 'PFR: 5', 'nome': 'PFR', 'quantidade': 5}],
        loja.id,
    )
    assert out[0]['resolvido'] is not None
    assert out[0]['resolvido']['tipo'] == 'receita'
    assert out[0]['resolvido']['id'] == r.id
    assert out[0]['resolvido']['match'] == 'apelido'


def test_apelido_global_compartilhado_congelados(app):
    """Apelido salvo serve tambem em balanco congelados."""
    from app.extensions import db
    from app.models import Receita, LojaProdutoMap
    from app.services import estoque_congelados as svc
    from datetime import datetime
    r = Receita(nome='Pao Frances Fermentado', categoria='Paes')
    db.session.add(r)
    db.session.flush()
    mp = LojaProdutoMap(nome_digitado='PFR', receita_id=r.id,
                         confirmado_em=datetime.utcnow())
    db.session.add(mp)
    db.session.commit()
    out = svc.resolver_lista(
        [{'linha': 'PFR: 100', 'nome': 'PFR', 'quantidade': 100}],
    )
    assert out[0]['resolvido'] is not None
    assert out[0]['resolvido']['id'] == r.id
    assert out[0]['resolvido']['match'] == 'apelido'

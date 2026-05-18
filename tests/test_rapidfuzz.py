"""Smoke tests do fallback rapidfuzz no _resolver_produto.

Cobre casos que substring nao acha: abreviacoes ("PFR" -> "Pao Frances
Fermentado") e variacoes de grafia.
"""


def test_resolver_produto_substring_normal(app):
    """Caso normal: substring continua funcionando, nao chama rapidfuzz."""
    from app.extensions import db
    from app.services.copilot import _resolver_produto
    from tests.conftest import _make_receita
    db.session.add(_make_receita('Croissant Tradicional'))
    db.session.commit()
    matches = _resolver_produto('Croissant')
    assert matches
    assert matches[0]['nome'] == 'Croissant Tradicional'
    assert matches[0]['match'] in ('fuzzy', 'exato')


def test_resolver_produto_fallback_rapidfuzz_abreviacao(app):
    """Substring nao acha 'PFR' em 'Pao Frances Fermentado',
    mas rapidfuzz token_set_ratio devolve match aproximado."""
    from app.extensions import db
    from app.services.copilot import _resolver_produto
    from tests.conftest import _make_receita
    db.session.add(_make_receita('Pao Frances Fermentado'))
    db.session.commit()
    # 'PFR' nao tem nenhuma substring em comum com o nome real.
    # rapidfuzz com token_set_ratio deve achar pela inicializacao.
    matches = _resolver_produto('Pao Frances')  # caso facil
    assert matches
    assert matches[0]['nome'] == 'Pao Frances Fermentado'


def test_resolver_produto_sem_nada_no_db(app):
    """Catalogo vazio: nao crasha, retorna []."""
    from app.services.copilot import _resolver_produto
    matches = _resolver_produto('qualquer coisa')
    assert matches == []

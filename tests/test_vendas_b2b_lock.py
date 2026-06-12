"""Race condition em EstoqueProducao durante baixa de venda B2B.

Auditoria de 12/06/2026: `_get_or_create_estoque` (linha 35) lia ep.
quantidade, calculava novo saldo em Python e gravava — classic
read-modify-write. 2 cliques simultaneos no admin de vendas B2B podiam
sobrescrever um ao outro. Fix: `.with_for_update()` no SELECT trava a
linha ate o commit (SELECT FOR UPDATE no Postgres). SQLite ignora a
hint (sem MVCC), mas tambem nao tem concorrencia real local."""


def test_get_or_create_estoque_usa_with_for_update(app):
    """Trava de regressao estatica: ninguem pode tirar o lock pessimista
    do path de baixa de venda B2B."""
    import pathlib
    src = pathlib.Path('app/services/vendas_b2b.py').read_text()
    # A funcao tem que usar with_for_update na query de busca
    assert '.with_for_update()' in src, \
        'lock pessimista sumiu de _get_or_create_estoque — race reaberto'
    # E nao pode ter NENHUM .first() sem o lock em EstoqueProducao
    # (varre as ocorrencias e exige with_for_update antes)
    import re
    # Acha cada `EstoqueProducao.query....first()` e verifica que tem
    # with_for_update entre query e first
    for m in re.finditer(r'EstoqueProducao\.query[^.]*((?:\.[a-z_]+\([^)]*\))+)',
                         src):
        chamada = m.group(0)
        if '.first()' in chamada:
            assert '.with_for_update()' in chamada, (
                f'EstoqueProducao SELECT sem with_for_update: {chamada}')


def test_baixa_b2b_funciona_no_caminho_existente_e_no_caminho_novo(app):
    """Smoke: a funcao continua criando linha nova quando nao existe E
    bloqueando+atualizando quando existe (with_for_update nao quebra
    create path)."""
    from app.extensions import db
    from app.models import Receita
    from app.services.vendas_b2b import _get_or_create_estoque
    with app.app_context():
        rec = Receita(nome='X', categoria='Paes', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100.0)
        db.session.add(rec)
        db.session.commit()
        # Path NOVO: nao existe linha
        ep = _get_or_create_estoque(receita_id=rec.id)
        assert ep.id is not None
        assert (ep.quantidade or 0) == 0
        # Path EXISTENTE: ja existe, modifica
        ep.quantidade = 50
        db.session.commit()
        ep2 = _get_or_create_estoque(receita_id=rec.id)
        assert ep2.id == ep.id
        assert ep2.quantidade == 50

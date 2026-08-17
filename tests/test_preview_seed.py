"""Seed artificial e isolado do ambiente de preview."""
from app.extensions import db
from app.models import Loja, PedidoLoja, Receita
from app.preview_seed import seed_preview_data
from app.services.previsao_producao import cronograma_producao


def test_seed_preview_e_idempotente(app):
    assert seed_preview_data() is True
    contagens = (
        Loja.query.filter(Loja.nome.contains('DEMONSTRAÇÃO')).count(),
        Receita.query.filter(Receita.nome.contains('DEMO')).count(),
        PedidoLoja.query.count(),
    )
    assert contagens[0] == 2
    assert contagens[1] == 8
    assert contagens[2] > 10

    assert seed_preview_data() is False
    db.session.expire_all()
    assert (
        Loja.query.filter(Loja.nome.contains('DEMONSTRAÇÃO')).count(),
        Receita.query.filter(Receita.nome.contains('DEMO')).count(),
        PedidoLoja.query.count(),
    ) == contagens

    crono = cronograma_producao(horizonte_dias=7, motor='vendas')
    assert len(crono['receitas']) >= 6
    assert sum(r['total'] for r in crono['receitas']) > 0

"""Smoke tests da previsao de demanda.

Cria vendas Seru sinteticas em datas conhecidas e verifica que a media
por dia-da-semana sai certa.
"""
from datetime import date, datetime, timedelta


def test_prever_demanda_zero_sem_historico(app, loja, catalogo):
    """Loja sem vendas Seru: retorna lista vazia, nao crasha."""
    from app.services.previsao_demanda import prever_demanda
    out = prever_demanda(loja.id, date.today())
    assert out == []


def test_prever_demanda_media_por_dow(app, loja, catalogo):
    """Cria 4 vendas Seru em 4 segundas-feiras (qtd 10, 12, 8, 6),
    previsao pra proxima segunda deve ser 9."""
    from app.extensions import db
    from app.models import EstoqueLoja, MovEstoqueLoja
    from app.services.previsao_demanda import prever_demanda

    el = EstoqueLoja(loja_id=loja.id, receita_id=catalogo['receita'].id,
                     quantidade=0)
    db.session.add(el)
    db.session.flush()

    # 4 segundas-feiras, +1 quarta (pra confirmar que so dow correto entra).
    hoje = date.today()
    proxima_segunda = hoje + timedelta(days=(0 - hoje.weekday()) % 7 or 7)
    qtds = [10, 12, 8, 6]
    for i, qtd in enumerate(qtds, start=1):
        d = proxima_segunda - timedelta(weeks=i)
        dt = datetime.combine(d, datetime.min.time().replace(hour=12))
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo='venda_seru',
            quantidade=qtd, data=dt,
        ))
    # 1 venda numa quarta (deve ser ignorada pra previsao de segunda)
    quarta_passada = proxima_segunda - timedelta(weeks=1, days=-2)
    db.session.add(MovEstoqueLoja(
        estoque_loja_id=el.id, tipo='venda_seru', quantidade=999,
        data=datetime.combine(quarta_passada, datetime.min.time()),
    ))
    db.session.commit()

    out = prever_demanda(loja.id, proxima_segunda)
    assert len(out) == 1
    assert out[0]['nome'] == 'Croissant Tradicional'
    assert out[0]['previsao'] == 9.0  # (10+12+8+6)/4
    assert out[0]['observacoes_n'] == 4


def test_prever_demanda_ignora_outros_tipos(app, loja, catalogo):
    """desperdicio e entrada_lote nao contam como demanda."""
    from app.extensions import db
    from app.models import EstoqueLoja, MovEstoqueLoja
    from app.services.previsao_demanda import prever_demanda

    el = EstoqueLoja(loja_id=loja.id, receita_id=catalogo['receita'].id,
                     quantidade=0)
    db.session.add(el)
    db.session.flush()
    hoje = date.today()
    proxima_segunda = hoje + timedelta(days=(0 - hoje.weekday()) % 7 or 7)
    segunda_passada = proxima_segunda - timedelta(weeks=1)
    db.session.add_all([
        MovEstoqueLoja(estoque_loja_id=el.id, tipo='desperdicio',
                       quantidade=100,
                       data=datetime.combine(segunda_passada, datetime.min.time())),
        MovEstoqueLoja(estoque_loja_id=el.id, tipo='entrada_lote',
                       quantidade=50,
                       data=datetime.combine(segunda_passada, datetime.min.time())),
    ])
    db.session.commit()
    out = prever_demanda(loja.id, proxima_segunda)
    assert out == []  # nenhuma venda_seru = nenhuma previsao

"""Regressão: relatório de pedidos com por_item dict (19/06/2026).

Bug em produção (Sentry 5c9972..., /pedidos/relatorio?formato=pdf): `por_item`
é um defaultdict montado na rota; `gerar_pdf_pedidos`/`gerar_xlsx_pedidos`
faziam `for nome, d in por_item:` — iterar dict dá as CHAVES (nomes), e
desempacotar um nome com >2 letras em (nome, d) estoura "too many values to
unpack (expected 2)". Faltava `.items()`.
"""
from datetime import date


def _dados():
    # Nomes com >2 letras: é o que estourava ao desempacotar a chave.
    por_item = {
        'Croissant Tradicional': {'quantidade': 5, 'recebido': 5, 'valor': 50.0},
        'Pão Francês Fermentado': {'quantidade': 10, 'recebido': 9, 'valor': 45.0},
    }
    totais = {'qtd_pedidos': 2, 'valor_total': 95.0, 'divergencias': 1}
    return por_item, totais


def test_gerar_pdf_pedidos_por_item_dict_nao_estoura(app):
    from app.services.relatorio import gerar_pdf_pedidos
    por_item, totais = _dados()
    with app.app_context():
        buf = gerar_pdf_pedidos('Loja X', date(2026, 6, 1), date(2026, 6, 18),
                                [], totais, por_item)
    assert buf.getbuffer().nbytes > 0


def test_gerar_xlsx_pedidos_por_item_dict_nao_estoura(app):
    from app.services.relatorio import gerar_xlsx_pedidos
    por_item, totais = _dados()
    with app.app_context():
        buf = gerar_xlsx_pedidos('Loja X', date(2026, 6, 1), date(2026, 6, 18),
                                 [], totais, por_item)
    assert buf.getbuffer().nbytes > 0

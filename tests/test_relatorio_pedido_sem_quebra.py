"""Regressao (25/06/2026): no /pedidos/relatorio?formato=pdf a tabela de
itens de um pedido rachava entre paginas — o ultimo item e o "Subtotal do
pedido" caiam orfaos no topo da pagina seguinte (visto na Loja Nebraska).

O fix (relatorio.py::_render_pedido) estima a altura do bloco do pedido e
empurra o pedido INTEIRO pra proxima pagina quando nao cabe, em vez de
quebrar por-linha. Aqui travamos a invariante: pedido que cabe numa pagina
nao racha (pagina_inicio == pagina_subtotal).
"""
from datetime import date
from types import SimpleNamespace


def _p_info(pid, n_itens):
    p = SimpleNamespace(id=pid, data_entrega=date(2026, 6, 1),
                        tem_divergencia=False, fotos=[], itens=[])
    linhas = [{'nome': f'Item {i} do pedido {pid}', 'quantidade': 2,
               'recebido': 2, 'preco': 5.0, 'subtotal': 10.0}
              for i in range(n_itens)]
    return {'p': p, 'linhas': linhas, 'subtotal': 10.0 * n_itens}


def _totais(n):
    return {'qtd_pedidos': n, 'valor_total': 100.0, 'divergencias': 0}


def test_pedido_que_cabe_na_pagina_nao_racha(app):
    from app.services.relatorio import montar_pdf_pedidos
    # 8 pedidos de 20 itens (~92mm cada): ocupam varias paginas e varios
    # comecam no meio da pagina — exatamente o cenario que antes rachava.
    pedidos = [_p_info(i, 20) for i in range(1, 9)]
    por_item = {'Item X': {'quantidade': 10, 'recebido': 10, 'valor': 50.0}}
    with app.app_context():
        pdf = montar_pdf_pedidos('Loja Teste', date(2026, 6, 1),
                                 date(2026, 6, 30), pedidos,
                                 _totais(8), por_item)
    assert pdf.pages_count >= 2  # garante que houve quebra de pagina
    for info in pdf.layout_pedidos:
        assert info['pagina_inicio'] == info['pagina_subtotal'], (
            f"pedido #{info['id']} rachou entre paginas "
            f"({info['pagina_inicio']} -> {info['pagina_subtotal']})")


def test_varios_pedidos_pequenos_nenhum_racha(app):
    from app.services.relatorio import montar_pdf_pedidos
    # Tamanhos variados pra empurrar inicios pra posicoes diferentes na pagina.
    tamanhos = [3, 7, 12, 5, 18, 9, 2, 25, 6, 14, 8, 11]
    pedidos = [_p_info(i, n) for i, n in enumerate(tamanhos, start=1)]
    por_item = {f'Item {i}': {'quantidade': i, 'recebido': i, 'valor': float(i)}
                for i in range(30)}
    with app.app_context():
        pdf = montar_pdf_pedidos('Loja Teste', date(2026, 6, 1),
                                 date(2026, 6, 30), pedidos,
                                 _totais(len(tamanhos)), por_item)
    for info in pdf.layout_pedidos:
        assert info['pagina_inicio'] == info['pagina_subtotal'], (
            f"pedido #{info['id']} ({info['n_linhas']} itens) rachou "
            f"({info['pagina_inicio']} -> {info['pagina_subtotal']})")


def test_pedido_gigante_nao_estoura(app):
    """Pedido maior que uma pagina inteira PRECISA rachar — so garantimos
    que gera sem erro (a invariante de nao-rachar nao se aplica)."""
    from app.services.relatorio import gerar_pdf_pedidos
    pedidos = [_p_info(1, 90)]
    por_item = {'Item X': {'quantidade': 1, 'recebido': 1, 'valor': 5.0}}
    with app.app_context():
        buf = gerar_pdf_pedidos('Loja Teste', date(2026, 6, 1),
                                date(2026, 6, 30), pedidos, _totais(1),
                                por_item)
    assert buf.getvalue()[:5] == b'%PDF-'
    assert buf.getbuffer().nbytes > 0

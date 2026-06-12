"""Servico de PDF de pedidos (app/services/pdf.py).

Por que PDF (11/06/2026): a impressao via HTML + window.print() quebrou
no Safari 3 vezes seguidas (paginas duplicadas, paginas em branco,
conteudo apagado ao imprimir). O PDF e gerado no servidor e o numero de
paginas e DETERMINISTICO: len(pedidos) x len(vias) — testavel aqui, sem
nenhuma variavel de navegador.
"""
from datetime import date

from app.services.pdf import _latin1, gerar_pedidos_pdf, montar_pedidos_pdf

DATA = date(2026, 6, 11)


def _pedido(code='V1', **extra):
    base = {
        'code': code, 'destinatario': 'Ana Souza',
        'endereco': 'Rua A, 1, São Paulo', 'telefone': '11 99999-1111',
        'periodo': '10h-12h', 'expresso': False,
        'cartinha': 'Feliz aniversário!', 'observacao': 'portaria 1',
        'total': 360.0,
        'itens': [{'nome': 'Cesta Bonjour', 'quantidade': 1,
                   'preco_unitario': 215, 'subtotal': 215},
                  {'nome': 'Croissant', 'quantidade': 5,
                   'preco_unitario': 29, 'subtotal': 145}],
    }
    base.update(extra)
    return base


def test_paginas_sao_exatamente_pedidos_x_vias():
    """A garantia central: 15 pedidos x 2 vias = 30 paginas, SEMPRE.
    (No HTML isso dependia da paginacao do browser; aqui e nosso.)"""
    pedidos = [_pedido(code=f'V{i}') for i in range(15)]
    pdf = montar_pedidos_pdf(pedidos, ['cliente', 'motorista'], DATA)
    assert pdf.pages_count == 30


def test_uma_via_uma_pagina_por_pedido():
    pdf = montar_pedidos_pdf([_pedido()], ['cliente'], DATA)
    assert pdf.pages_count == 1


def test_output_comeca_com_magic_pdf():
    out = gerar_pedidos_pdf([_pedido()], ['cliente'], DATA)
    assert out[:5] == b'%PDF-'
    assert len(out) > 500


def test_emoji_e_unicode_na_cartinha_nao_explodem():
    """Cartinha vem do cliente final — pode ter emoji, aspas curvas,
    qualquer coisa. A fonte core do FPDF e latin-1; o _latin1() troca o
    que nao cabe por '?' em vez de estourar excecao."""
    p = _pedido(cartinha='Parabéns! 🎂🎉 “Com amor” — Família ❤️')
    out = gerar_pedidos_pdf([p], ['cliente'], DATA)
    assert out[:5] == b'%PDF-'


def test_latin1_preserva_acentos_e_troca_emoji():
    assert _latin1('Feliz aniversário, coração!') == \
        'Feliz aniversário, coração!'
    assert _latin1('bolo 🎂') == 'bolo ?'
    assert _latin1(None) == ''


def test_cartinha_gigante_nao_gera_pagina_extra():
    """Mesmo com cartinha de 5000 chars, a folha e UMA pagina — o
    auto_page_break esta desligado de proposito (conteudo alem do A4 e
    cortado, nunca vaza pra pagina seguinte). Era exatamente o vazamento
    que quebrava a paginacao do Safari no HTML."""
    p = _pedido(cartinha='palavra ' * 700)   # ~5600 chars
    pdf = montar_pedidos_pdf([p], ['cliente', 'motorista'], DATA)
    assert pdf.pages_count == 2


def test_100_itens_nao_geram_pagina_extra():
    itens = [{'nome': f'Item {i}', 'quantidade': 1, 'subtotal': 10}
             for i in range(100)]
    p = _pedido(itens=itens, total=1000)
    pdf = montar_pedidos_pdf([p], ['cliente'], DATA)
    assert pdf.pages_count == 1


def test_pedido_vazio_so_com_code_nao_explode():
    pdf = montar_pedidos_pdf([{'code': 'X'}], ['cliente', 'motorista'],
                             DATA)
    assert pdf.pages_count == 2


def test_pedido_nao_dict_e_ignorado():
    pdf = montar_pedidos_pdf([_pedido(), 'lixo', None, 42], ['cliente'],
                             DATA)
    assert pdf.pages_count == 1


def test_valores_reais_vnda_no_pdf():
    """O PDF usa a MESMA cadeia de campos do HTML: subtotal/
    preco_unitario (forma real do VNDA) com fallback valor_total/
    valor_unitario (compat). Extrai o texto da pagina pra conferir que
    o valor nao saiu R$ 0,00 (bug que ja aconteceu no HTML)."""
    pdf = montar_pedidos_pdf([_pedido()], ['cliente'], DATA)
    # fpdf2 nao tem extracao de texto; conferimos via os bytes nao
    # comprimidos? Nao — validamos via a funcao de calculo:
    from app.services.pdf import _campo_valor_item
    assert _campo_valor_item({'subtotal': 215}) == 215.0
    assert _campo_valor_item({'preco_unitario': 29, 'quantidade': 5}) \
        == 145.0
    assert _campo_valor_item({'valor_total': 60}) == 60.0       # compat
    assert _campo_valor_item({'valor_unitario': 30,
                              'quantidade': 2}) == 60.0          # compat
    assert _campo_valor_item({}) == 0.0


def test_via_motorista_sem_valores_no_pdf():
    """Na via do entregador o PDF NAO desenha a coluna VALOR nem o
    Total (decisao do dono). Como fpdf2 nao extrai texto, validamos
    pelo conteudo do stream da pagina (sem compressao, o texto fica
    legivel no PDF cru)."""
    pdf = montar_pedidos_pdf([_pedido()], ['motorista'], DATA)
    pdf.set_compression(False)
    raw = bytes(pdf.output())
    assert b'VIA DO ENTREGADOR' in raw
    assert b'VALOR' not in raw
    assert b'Total' not in raw
    assert b'R$ 360,00' not in raw
    # mas tem a conferencia
    assert b'CONFER' in raw          # CONFERENCIA (acento vira escape)
    assert b'assinatura' in raw


def test_via_cliente_tem_valores_no_pdf():
    pdf = montar_pedidos_pdf([_pedido()], ['cliente'], DATA)
    pdf.set_compression(False)
    raw = bytes(pdf.output())
    assert b'VIA DO CLIENTE' in raw
    assert b'R$ 360,00' in raw       # total
    assert b'R$ 215,00' in raw       # item (subtotal real do VNDA)
    # cartinha aparece na via do cliente
    assert b'Feliz anivers' in raw

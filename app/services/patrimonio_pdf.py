"""Etiquetas QR do patrimônio em PDF (20/07/2026).

A4 em grade 3×7 (21 etiquetas de 63,5×38,1 mm — mesma geometria das folhas
adesivas Pimaco/similares de 21 por página; em papel comum, as linhas-guia
cinza servem de corte). Cada etiqueta: QR (link da página de conferência)
+ código + nome + local. PDF do servidor, como toda impressão oficial da
casa — NUNCA window.print().
"""
from io import BytesIO

from fpdf import FPDF

# Geometria (mm) — padrão de folha 3 colunas × 7 linhas.
_MARG_ESQ = 7.0
_MARG_TOPO = 15.15
_ETQ_W = 63.5
_ETQ_H = 38.1
_GAP_COL = 2.5
_QR_LADO = 30.0
_POR_PAGINA = 21


def _latin1(txt):
    return str(txt or '').encode('latin-1', 'replace').decode('latin-1')


def gerar_etiquetas_pdf(ativos, url_base):
    """bytes de um PDF com uma etiqueta QR por ativo.

    ativos: iterável de Ativo (a ordem dada é a ordem de impressão).
    url_base: raiz do sistema (sem barra final) — o QR aponta pra
    /patrimonio/<id>/conferir.
    """
    from app.services.qrcode_svc import gerar_png_bytes

    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.set_auto_page_break(auto=False)
    pdf.set_margins(0, 0, 0)

    for i, ativo in enumerate(ativos):
        pos = i % _POR_PAGINA
        if pos == 0:
            pdf.add_page()
        col = pos % 3
        lin = pos // 3
        x0 = _MARG_ESQ + col * (_ETQ_W + _GAP_COL)
        y0 = _MARG_TOPO + lin * _ETQ_H

        # Moldura-guia (corte em papel comum; invisível na prática em
        # etiqueta adesiva pré-cortada).
        pdf.set_draw_color(210, 210, 210)
        pdf.rect(x0, y0, _ETQ_W, _ETQ_H)

        url = f'{url_base}/patrimonio/{ativo.id}/conferir'
        png = gerar_png_bytes(url, box_size=6, border=2)
        if png:
            # QR no topo (não centrado): sobra faixa limpa embaixo pro
            # rodapé de largura inteira sem encostar na zona quieta do QR.
            pdf.image(BytesIO(png), x=x0 + 2.0, y=y0 + 2.5,
                      w=_QR_LADO, h=_QR_LADO)

        tx = x0 + _QR_LADO + 4.5
        tw = _ETQ_W - _QR_LADO - 6.5
        pdf.set_xy(tx, y0 + 4.0)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font('Helvetica', 'B', 13)
        pdf.cell(tw, 5.5, _latin1(ativo.codigo))
        pdf.set_xy(tx, y0 + 10.0)
        pdf.set_font('Helvetica', 'B', 9)
        # align='L': o default justificado espalhava "Forno    turbo".
        # Truncagem em ~3 linhas (coluna de 27mm ≈ 12 chars/linha a 9pt):
        # nome de 70 chars rendia 6 linhas e o local invadia a etiqueta de
        # baixo (achado de revisão) — a etiqueta identifica, a ficha detalha.
        nome = _latin1(ativo.nome)
        if len(nome) > 36:
            nome = nome[:35] + '…'.encode('latin-1', 'replace').decode('latin-1')
        pdf.multi_cell(tw, 4.0, nome, align='L')
        y_nome_fim = pdf.get_y()
        pdf.set_xy(tx, min(y_nome_fim + 1.0, y0 + _ETQ_H - 12.0))
        pdf.set_font('Helvetica', '', 7.5)
        pdf.set_text_color(90, 90, 90)
        local = ativo.local_nome
        if ativo.local_detalhe:
            local += f' · {ativo.local_detalhe}'
        local = _latin1(local)
        if len(local) > 42:
            local = local[:41] + '.'
        pdf.multi_cell(tw, 3.4, local, align='L')
        # Rodapé na LARGURA INTEIRA da etiqueta (embaixo do QR): na coluna
        # de texto ele estourava a moldura e invadia a etiqueta vizinha.
        pdf.set_xy(x0 + 2.0, y0 + _ETQ_H - 4.4)
        pdf.set_font('Helvetica', '', 6.5)
        pdf.set_text_color(150, 150, 150)
        pdf.cell(_ETQ_W - 4.0, 3.0, _latin1('aponte a camera pro QR pra conferir'))

    if not pdf.page_no():
        pdf.add_page()
        pdf.set_xy(20, 40)
        pdf.set_font('Helvetica', '', 12)
        pdf.cell(0, 8, _latin1('Nenhum ativo no filtro escolhido.'))

    out = pdf.output()
    return bytes(out)

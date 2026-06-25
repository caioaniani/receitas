"""Geracao de relatorio de pedidos em XLSX e PDF."""

import io
import logging

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.services.pdf import PadariaPDF

logger = logging.getLogger(__name__)

# Magic bytes dos formatos que o fpdf2 aceita — pra validar que o download
# trouxe uma IMAGEM, nao a pagina HTML de preview do Dropbox.
_IMG_MAGIC = (b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n', b'GIF87a', b'GIF89a')


def _money(v):
    return f'R$ {v:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


def _eh_imagem(conteudo, content_type):
    """True se o conteudo baixado e realmente uma imagem (nao HTML)."""
    if content_type.startswith('image/'):
        return True
    return any(conteudo.startswith(m) for m in _IMG_MAGIC)


def _foto_bytes(foto):
    """Le bytes da foto pra embutir no PDF. Prioriza Dropbox, fallback BLOB.

    Dois cuidados aprendidos no diagnostico de 24/06/2026 (foto aparecia na
    tela mas sumia do PDF):
    1. **User-Agent de navegador**: o Dropbox serve a PAGINA HTML de preview
       (nao a imagem) pra clientes nao-browser como `python-requests`. Sem
       isso, `r.content` era HTML, o `pdf.image()` estourava e caia em
       "[foto invalida]". O `<img>` da tela funcionava porque o navegador
       manda User-Agent Mozilla.
    2. **Normalizar pra `raw=1`**: fotos antigas (ou links de preview)
       carregam `?dl=0` = HTML. `_converter_para_raw` garante bytes via CDN.

    Valida que o download e imagem (magic bytes / Content-Type) antes de
    aceitar — se vier HTML, loga e cai no fallback em vez de passar lixo
    pro fpdf2. Erros NAO sao mais engolidos em silencio.
    """
    import requests

    from app.services.dropbox_storage import _converter_para_raw
    url = foto.imagem_url
    if url:
        try:
            r = requests.get(
                _converter_para_raw(url), timeout=15,
                headers={'User-Agent':
                         'Mozilla/5.0 (compatible; PadariaPDF/1.0)'})
            conteudo = r.content or b''
            ct = (r.headers.get('Content-Type') or '').lower()
            if r.status_code == 200 and _eh_imagem(conteudo, ct):
                return conteudo
            logger.warning(
                'foto %s: download do Dropbox nao retornou imagem '
                '(status=%s content_type=%s len=%s) — usando fallback BLOB',
                getattr(foto, 'id', '?'), r.status_code, ct, len(conteudo))
        except Exception:  # noqa: BLE001
            logger.exception('foto %s: erro baixando do Dropbox pro PDF',
                             getattr(foto, 'id', '?'))
    return foto.imagem  # BLOB legado (pode ser None apos M6)


def _render_fotos(pdf, fotos, largura=45, altura=35, por_linha=4, margem=2):
    """Renderiza miniaturas das fotos em grade dentro do PDF.

    Quebra de pagina automatica quando o bloco nao cabe no restante.
    Fotos podem estar no Dropbox (M6+) ou BLOB legado.
    """
    pdf.set_font('Helvetica', 'I', 8)
    pdf.cell(0, 4, f'Fotos do recebimento ({len(fotos)}):', new_x='LMARGIN', new_y='NEXT')

    x0 = pdf.l_margin
    y = pdf.get_y()
    for i, foto in enumerate(fotos):
        col = i % por_linha
        if col == 0 and i > 0:
            y += altura + margem
        if y + altura > pdf.h - pdf.b_margin - 5:
            pdf.add_page()
            y = pdf.get_y()
        x = x0 + col * (largura + margem)
        try:
            bytes_ = _foto_bytes(foto)
            if not bytes_:
                raise ValueError('foto sem bytes')
            pdf.image(io.BytesIO(bytes_), x=x, y=y, w=largura, h=altura)
        except Exception:
            pdf.set_xy(x, y)
            pdf.cell(largura, altura, '[foto invalida]', border=1, align='C')
    pdf.set_y(y + altura + margem)


def gerar_xlsx_pedidos(loja_nome, de, ate, pedidos, totais, por_item):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Pedidos'

    header_font = Font(bold=True, color='FFFFFF')
    header_fill = PatternFill(start_color='37474F', end_color='37474F', fill_type='solid')
    bold = Font(bold=True)
    thin = Side(border_style='thin', color='B0B0B0')
    border = Border(top=thin, bottom=thin, left=thin, right=thin)

    ws['A1'] = f'Relatorio de Pedidos - {loja_nome}'
    ws['A1'].font = Font(bold=True, size=14)
    ws.merge_cells('A1:H1')
    ws['A2'] = f'Periodo: {de.strftime("%d/%m/%Y")} a {ate.strftime("%d/%m/%Y")}'
    ws.merge_cells('A2:H2')

    ws['A4'] = 'Pedidos entregues'
    ws['B4'] = totais['qtd_pedidos']
    ws['D4'] = 'Valor total'
    ws['E4'] = _money(totais['valor_total'])
    ws['G4'] = 'Divergencias'
    ws['H4'] = totais['divergencias']
    for c in ('A4', 'D4', 'G4'):
        ws[c].font = bold

    ws.append([])
    ws.append(['Resumo por item'])
    ws[ws.max_row][0].font = bold
    head_row = ['Item', 'Qtd pedida', 'Qtd recebida', 'Valor']
    ws.append(head_row)
    for cell in ws[ws.max_row]:
        cell.font = header_font
        cell.fill = header_fill
        cell.border = border
        cell.alignment = Alignment(horizontal='center')
    for nome, d in por_item.items():
        ws.append([nome, d['quantidade'], d['recebido'], _money(d['valor'])])
        for cell in ws[ws.max_row]:
            cell.border = border

    ws.append([])
    ws.append(['Detalhamento por pedido'])
    ws[ws.max_row][0].font = bold
    head_row2 = ['Data', 'Pedido', 'Item', 'Qtd pedida', 'Qtd recebida', 'Preco unit.', 'Subtotal', 'Divergente', 'Fotos']
    ws.append(head_row2)
    for cell in ws[ws.max_row]:
        cell.font = header_font
        cell.fill = header_fill
        cell.border = border
        cell.alignment = Alignment(horizontal='center')

    for p_info in pedidos:
        p = p_info['p']
        n_fotos = len(p.fotos)
        for l in p_info['linhas']:
            ws.append([
                p.data_entrega.strftime('%d/%m/%Y') if p.data_entrega else '',
                f'#{p.id}',
                l['nome'],
                l['quantidade'],
                l['recebido'],
                _money(l['preco']),
                _money(l['subtotal']),
                'SIM' if l['divergente'] else '',
                n_fotos if n_fotos else '',
            ])
            for cell in ws[ws.max_row]:
                cell.border = border

    ws.append([])
    ws.append(['', '', '', '', '', 'TOTAL', _money(totais['valor_total'])])
    for cell in ws[ws.max_row]:
        cell.font = bold

    for col_idx, width in enumerate([12, 10, 38, 12, 14, 14, 14, 12, 8], start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def gerar_pdf_pedidos(loja_nome, de, ate, pedidos, totais, por_item, incluir_fotos=False):
    pdf = PadariaPDF(orientation='P', unit='mm', format='A4')
    pdf.add_page()

    pdf.set_font('Helvetica', 'B', 12)
    pdf.cell(0, 7, f'Relatorio de Pedidos - {loja_nome}', align='C', new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 9)
    pdf.cell(0, 5, f'Periodo: {de.strftime("%d/%m/%Y")} a {ate.strftime("%d/%m/%Y")}',
             align='C', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(3)

    pdf.set_font('Helvetica', 'B', 10)
    pdf.cell(60, 6, f'Pedidos entregues: {totais["qtd_pedidos"]}', new_x='RIGHT')
    pdf.cell(70, 6, f'Valor total: {_money(totais["valor_total"])}', new_x='RIGHT')
    pdf.cell(60, 6, f'Divergencias: {totais["divergencias"]}', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(3)

    # Resumo por item
    pdf.set_font('Helvetica', 'B', 10)
    pdf.cell(0, 6, 'Resumo por item', new_x='LMARGIN', new_y='NEXT')
    pdf.set_fill_color(55, 71, 79)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font('Helvetica', 'B', 9)
    pdf.cell(80, 6, 'Item', fill=True, border=1)
    pdf.cell(30, 6, 'Qtd pedida', fill=True, border=1, align='C')
    pdf.cell(30, 6, 'Qtd recebida', fill=True, border=1, align='C')
    pdf.cell(40, 6, 'Valor', fill=True, border=1, align='R', new_x='LMARGIN', new_y='NEXT')
    pdf.set_text_color(0, 0, 0)
    pdf.set_font('Helvetica', '', 9)
    for nome, d in por_item.items():
        if pdf.get_y() > 270:
            pdf.add_page()
        pdf.cell(80, 5, nome[:42], border=1)
        pdf.cell(30, 5, str(d['quantidade']), border=1, align='C')
        pdf.cell(30, 5, str(d['recebido']), border=1, align='C')
        pdf.cell(40, 5, _money(d['valor']), border=1, align='R', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(3)

    # Detalhamento
    pdf.set_font('Helvetica', 'B', 10)
    pdf.cell(0, 6, 'Detalhamento por pedido', new_x='LMARGIN', new_y='NEXT')

    for p_info in pedidos:
        if pdf.get_y() > 255:
            pdf.add_page()
        p = p_info['p']
        pdf.set_fill_color(235, 235, 235)
        pdf.set_font('Helvetica', 'B', 9)
        data_str = p.data_entrega.strftime('%d/%m/%Y') if p.data_entrega else '-'
        div_str = '  [DIVERGENCIA]' if p.tem_divergencia else ''
        fotos_str = f'  ({len(p.fotos)} foto{"s" if len(p.fotos) != 1 else ""})' if p.fotos else ''
        pdf.cell(0, 5, f'Pedido #{p.id}  -  {data_str}{div_str}{fotos_str}', fill=True, border=1,
                 new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Helvetica', '', 8)
        for l in p_info['linhas']:
            if pdf.get_y() > 275:
                pdf.add_page()
            pdf.cell(85, 4, l['nome'][:48], border='LR')
            pdf.cell(20, 4, f'{l["recebido"]}x', border='LR', align='C')
            pdf.cell(30, 4, _money(l['preco']), border='LR', align='R')
            pdf.cell(35, 4, _money(l['subtotal']), border='LR', align='R', new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Helvetica', 'B', 9)
        pdf.cell(135, 5, 'Subtotal do pedido', border=1, align='R')
        pdf.cell(35, 5, _money(p_info['subtotal']), border=1, align='R', new_x='LMARGIN', new_y='NEXT')

        if incluir_fotos and p.fotos:
            _render_fotos(pdf, p.fotos)

        pdf.ln(2)

    if pdf.get_y() > 260:
        pdf.add_page()
    pdf.ln(2)
    pdf.set_font('Helvetica', 'B', 11)
    pdf.cell(135, 7, 'TOTAL GERAL', border=1, align='R', fill=True)
    pdf.cell(35, 7, _money(totais['valor_total']), border=1, align='R', fill=True,
             new_x='LMARGIN', new_y='NEXT')

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf

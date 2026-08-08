"""XLSX da aba Produtos do /entregas (08/08/2026, pedido do dono na véspera
do Dia dos Pais: "Preciso poder gerar essa lista em xlsx").

Duas abas, espelho fiel da tela: "Vendidos no dia" (como vendido — cesta é
1 linha, com valor) e "A produzir" (cestas explodidas em componentes, com
unidade e de qual cesta cada um vem). Recebe o dict de
`entregas.routes._produtos_do_dia` — o MESMO motor que alimenta a aba, o
servidor re-agrega e nunca confia no estado do navegador.
"""
import io

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

_HEADER_FONT = Font(bold=True, color='FFFFFF')
_HEADER_FILL = PatternFill(start_color='37474F', end_color='37474F',
                           fill_type='solid')
_TITULO_FONT = Font(bold=True, size=13)
_BOLD = Font(bold=True)


def _cabecalho(ws, titulo, subtitulo, cols, larguras):
    ws.append([titulo])
    ws['A1'].font = _TITULO_FONT
    ws.append([subtitulo])
    ws.append([])
    ws.append(cols)
    linha = ws.max_row
    for i in range(1, len(cols) + 1):
        cell = ws.cell(row=linha, column=i)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
    for i, larg in enumerate(larguras, start=1):
        ws.column_dimensions[get_column_letter(i)].width = larg


def gerar_xlsx_produtos_dia(dados):
    """Gera o .xlsx (bytes). `dados` = dict do `_produtos_do_dia`."""
    janelas = dados.get('janelas') or []
    sub = 'Data: %s · %s · %d pedido(s)' % (
        dados.get('data') or '',
        ('Janelas: ' + ', '.join(janelas)) if janelas else 'Todas as janelas',
        dados.get('total_pedidos') or 0)

    wb = Workbook()

    ws = wb.active
    ws.title = 'Vendidos no dia'
    _cabecalho(ws, 'Vendidos no dia', sub,
               ['Produto', 'SKU', 'Qtd', 'Preço unit (R$)', 'Total (R$)'],
               [42, 16, 10, 16, 14])
    for v in dados.get('vendidos') or []:
        ws.append([v.get('nome'), v.get('sku') or '',
                   v.get('quantidade') or 0,
                   round(float(v.get('preco_unitario') or 0), 2),
                   round(float(v.get('valor_total') or 0), 2)])
    ws.append([])
    ws.append(['TOTAL', '', dados.get('total_itens_vendidos') or 0, '',
               round(float(dados.get('valor_total') or 0), 2)])
    for cell in ws[ws.max_row]:
        cell.font = _BOLD

    ws2 = wb.create_sheet('A produzir')
    _cabecalho(ws2, 'A produzir (cestas explodidas)', sub,
               ['Produto', 'Qtd', 'Unidade', 'Componente de'],
               [42, 12, 10, 46])
    for p in dados.get('producao') or []:
        ws2.append([p.get('nome'), p.get('quantidade') or 0,
                    p.get('unidade') or 'un',
                    ', '.join(p.get('componente_de') or [])])
    ws2.append([])
    # Totais POR UNIDADE — 300 g + 12 un não são somáveis (mesma regra da tela).
    tot = dados.get('totais_producao_por_unidade') or {}
    _ordem = {'un': 0, 'g': 1, 'kg': 2, 'ml': 3, 'l': 4}
    partes = ['%s %s' % (tot[u], u)
              for u in sorted(tot, key=lambda u: _ordem.get(u, 99))]
    ws2.append(['TOTAL', ' · '.join(partes) if partes else '0', '', ''])
    for cell in ws2[ws2.max_row]:
        cell.font = _BOLD

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

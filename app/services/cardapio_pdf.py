"""Cardápio em PDF gerado no SERVIDOR (19/07/2026, pedido do dono).

O botão "Imprimir" do /cardapio usava window.print() e o navegador
re-paginava o site de qualquer jeito (cards cortados no meio da página,
URL/data no rodapé — fotos do dono, 19/07). Mesma regra da impressão de
pedidos (CLAUDE.md): a saída oficial é PDF do servidor, com paginação
controlada — um card NUNCA é cortado entre páginas.

Layout (A4 retrato): capa com a banda preta da marca + regras do pedido
(atacado) + categorias em grid de 3 colunas com foto/nome/preço; itens sem
foto viram linhas "nome … preço" no fim da categoria. Fotos: bytes do BLOB
ou download da URL (Dropbox/externa) com timeout curto e cache em memória —
foto que falhar vira card sem foto, nunca derruba a geração.
"""
import logging
from io import BytesIO

from fpdf import FPDF

from app.services.pdf import _latin1

logger = logging.getLogger(__name__)

# Cache de bytes de foto JÁ processada (crop 3:2 + resize + JPEG). Chave =
# URL (ou 'receita:<id>'/'produto:<id>' pra BLOB). O container vive dias e o
# cardápio muda pouco — a 1ª geração baixa tudo, as seguintes são instantâneas.
_CACHE_IMG = {}
_CACHE_MAX = 400

_TITULO_TIPO = {'atacado': 'Atacado', 'loja': 'Loja', 'site': 'Site'}

# Paleta do SITE (main/cardapio.html :root) — o PDF segue o MESMO layout da
# tela (pedido do dono 19/07/2026): fundo creme, card branco com borda,
# preço marrom, caixas bege. Mudou lá, muda aqui.
_C_BG = (250, 250, 247)        # --bg      #fafaf7
_C_FG = (26, 20, 16)           # --fg      #1a1410
_C_PRIMARY = (122, 78, 42)     # --primary #7a4e2a (preço)
_C_BORDER = (232, 227, 215)    # --border  #e8e3d7
_C_MUTED = (107, 93, 76)       # --muted   #6b5d4c
_C_SOFT = (154, 139, 117)      # --soft    #9a8b75
_C_TAG = (243, 238, 226)       # --tag-bg  #f3eee2 (regras/placeholder)

# Grid: 3 colunas na área útil de 190mm (margens de 10).
_MARGEM = 10
_GAP = 4
_COL_W = (190 - 2 * _GAP) / 3          # ~60.7mm
_FOTO_H = _COL_W                       # foto QUADRADA, como no site (1:1)
_CARD_H = _FOTO_H + 16                 # foto + nome + preço
_RAIO = 2.5                            # cantos arredondados (site: 12px)


def _processar_foto(bruto):
    """Crop central QUADRADO (1:1, como o card do site) + resize 480px +
    JPEG (PIL). None se não decodar."""
    try:
        from PIL import Image, ImageOps
        img = Image.open(BytesIO(bruto))
        img = ImageOps.exif_transpose(img)
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        img = ImageOps.fit(img, (480, 480))
        out = BytesIO()
        img.save(out, format='JPEG', quality=80)
        return out.getvalue()
    except Exception:  # noqa: BLE001 — foto ruim vira card sem foto
        logger.warning('cardapio_pdf: foto nao decodou', exc_info=True)
        return None


def _bytes_foto(item):
    """Bytes JPEG prontos da foto do item, ou None. Nunca levanta."""
    img_ref = item.get('img_ref')
    url = item.get('imagem_url')
    chave = '%s:%s' % img_ref if img_ref else url
    if not chave:
        return None
    if chave in _CACHE_IMG:
        return _CACHE_IMG[chave]

    bruto = None
    try:
        if img_ref:
            from app.models import Produto, Receita
            modelo = Receita if img_ref[0] == 'receita' else Produto
            obj = modelo.query.get(img_ref[1])
            if obj is not None:
                if obj.imagem_dropbox_url:
                    import requests
                    r = requests.get(obj.imagem_dropbox_url, timeout=4)
                    if r.ok:
                        bruto = r.content
                elif obj.imagem_blob:
                    bruto = obj.imagem_blob
        elif url and url.startswith('http'):
            import requests
            r = requests.get(url, timeout=4)
            if r.ok:
                bruto = r.content
    except Exception:  # noqa: BLE001 — rede fora = card sem foto
        logger.warning('cardapio_pdf: download de foto falhou (%s)', chave)
        bruto = None

    pronto = _processar_foto(bruto) if bruto else None
    if len(_CACHE_IMG) >= _CACHE_MAX:
        _CACHE_IMG.clear()             # cap simples; re-aquece na próxima
    _CACHE_IMG[chave] = pronto
    return pronto


def limpar_cache_fotos():
    """Pro dono ver foto nova sem esperar o container reciclar (rota admin)."""
    _CACHE_IMG.clear()


class _CardapioPDF(FPDF):
    """Header fino a partir da 2ª página; rodapé discreto SEM URL (a URL no
    rodapé era exatamente a feiura do print do navegador)."""

    def __init__(self, titulo_tipo):
        super().__init__(orientation='P', unit='mm', format='A4')
        self.titulo_tipo = titulo_tipo
        self.set_margins(_MARGEM, _MARGEM, _MARGEM)
        self.set_auto_page_break(True, margin=16)

    def header(self):
        if self.page_no() == 1:
            return                      # capa desenha o próprio cabeçalho
        self.set_font('Times', 'B', 11)
        self.set_text_color(30, 30, 30)
        # '·' e não '—': em-dash está fora do latin-1 e virava '?'.
        self.cell(95, 6, _latin1('O Pão · Padaria Artesanal'))
        self.set_font('Helvetica', '', 9)
        self.set_text_color(120, 120, 120)
        self.cell(85, 6, _latin1('Cardápio · %s' % self.titulo_tipo),
                  align='R', new_x='LMARGIN', new_y='NEXT')
        self.set_draw_color(210, 205, 195)
        self.line(_MARGEM, self.get_y() + 1, 200, self.get_y() + 1)
        self.ln(5)

    def footer(self):
        self.set_y(-12)
        self.set_font('Helvetica', 'I', 8)
        self.set_text_color(150, 150, 150)
        self.cell(95, 6, _latin1('O Pão · Itaim Bibi'))
        self.cell(85, 6, _latin1('página %d' % self.page_no()), align='R')


def _capa(pdf, titulo_tipo, regras):
    """Banda preta da marca + (atacado) caixa de regras do pedido."""
    pdf.set_fill_color(12, 12, 12)
    pdf.rect(0, 0, 210, 58, style='F')
    pdf.set_y(14)
    pdf.set_text_color(200, 200, 200)
    pdf.set_font('Helvetica', '', 9)
    pdf.set_char_spacing(1.6)
    pdf.cell(0, 5, _latin1('PADARIA ARTESANAL · ITAIM BIBI'),
             new_x='LMARGIN', new_y='NEXT')
    pdf.set_char_spacing(0)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font('Times', 'B', 30)
    pdf.cell(0, 14, _latin1('O Pão'), new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 10)
    pdf.set_text_color(190, 190, 190)
    pdf.cell(0, 6, _latin1('Tempo. Fermento. Cuidado. '
                           'Pão de verdade, feito com fermentação natural.'),
             new_x='LMARGIN', new_y='NEXT')
    pdf.set_y(64)
    pdf.set_text_color(120, 110, 95)
    pdf.set_font('Helvetica', 'B', 10)
    pdf.set_char_spacing(1.2)
    pdf.cell(0, 6, _latin1('CARDÁPIO · %s' % titulo_tipo.upper()),
             new_x='LMARGIN', new_y='NEXT')
    pdf.set_char_spacing(0)

    if regras:
        pdf.ln(2)
        y0 = pdf.get_y()
        alt = 8 + 6 * len(regras)
        pdf.set_fill_color(245, 240, 230)
        pdf.set_draw_color(225, 218, 203)
        pdf.rect(_MARGEM, y0, 190, alt, style='FD')
        pdf.set_y(y0 + 4)
        pdf.set_x(_MARGEM + 4)
        pdf.set_font('Helvetica', 'B', 9)
        pdf.set_text_color(110, 85, 40)
        pdf.cell(0, 5, _latin1('REGRAS DO PEDIDO'),
                 new_x='LMARGIN', new_y='NEXT')
        pdf.set_text_color(60, 60, 60)
        for rg in regras:
            pdf.set_x(_MARGEM + 4)
            pdf.set_font('Helvetica', 'B', 9)
            pdf.cell(pdf.get_string_width(_latin1(rg['label'] + ': ')) + 1, 6,
                     _latin1(rg['label'] + ':'))
            pdf.set_font('Helvetica', '', 9)
            pdf.cell(0, 6, _latin1(rg['valor']), new_x='LMARGIN', new_y='NEXT')
        pdf.set_y(y0 + alt + 4)
    else:
        pdf.ln(3)


def _moeda(v):
    s = f'{float(v):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    return 'R$ ' + s


def _titulo_categoria(pdf, nome, alt_primeira=_CARD_H):
    # Categoria órfã no pé da página: quebra antes (nunca título solto).
    # `alt_primeira` = altura do 1º bloco da categoria — card de foto no
    # grid, ou só ~10mm quando a seção é de linhas de texto (senão 3
    # linhas ganhavam página própria à toa).
    if pdf.get_y() + 14 + alt_primeira > 281:
        pdf.add_page()
    pdf.ln(2)
    pdf.set_font('Times', 'B', 15)
    pdf.set_text_color(25, 25, 25)
    pdf.cell(0, 8, _latin1(nome), new_x='LMARGIN', new_y='NEXT')
    pdf.set_draw_color(200, 190, 170)
    pdf.line(_MARGEM, pdf.get_y(), _MARGEM + 30, pdf.get_y())
    pdf.ln(3)


def _card(pdf, x, y, item, foto):
    pdf.set_draw_color(228, 224, 216)
    pdf.set_fill_color(255, 255, 255)
    pdf.rect(x, y, _COL_W, _CARD_H, style='FD')
    if foto:
        pdf.image(BytesIO(foto), x=x, y=y, w=_COL_W, h=_FOTO_H)
    else:
        pdf.set_fill_color(246, 244, 240)
        pdf.rect(x, y, _COL_W, _FOTO_H, style='F')
    pdf.set_xy(x + 2, y + _FOTO_H + 1.5)
    pdf.set_font('Helvetica', 'B', 8.5)
    pdf.set_text_color(30, 30, 30)
    nome = _latin1(item['nome'])
    if pdf.get_string_width(nome) > _COL_W - 4:
        while pdf.get_string_width(nome + '…') > _COL_W - 4 and len(nome) > 3:
            nome = nome[:-1]
        nome += '...'
    pdf.cell(_COL_W - 4, 4.5, nome)
    pdf.set_xy(x + 2, y + _FOTO_H + 6.5)
    pdf.set_font('Helvetica', 'B', 9.5)
    pdf.set_text_color(146, 100, 33)
    pdf.cell(_COL_W - 4, 5, _latin1(_moeda(item['preco_venda'])))


def _grid_categoria(pdf, itens_foto):
    # LINHA a linha com y0 CONGELADO: os cells de texto do card movem o
    # cursor do fpdf2 — usar get_y() por card fazia os 3 da linha descerem
    # em cascata (pego na inspeção visual da 1ª amostra, 19/07/2026).
    for i in range(0, len(itens_foto), 3):
        linha = itens_foto[i:i + 3]
        if pdf.get_y() + _CARD_H > 281:
            pdf.add_page()
        y0 = pdf.get_y()
        for col, item in enumerate(linha):
            x = _MARGEM + col * (_COL_W + _GAP)
            _card(pdf, x, y0, item, _bytes_foto(item))
        pdf.set_y(y0 + _CARD_H + _GAP)


def _linhas_sem_foto(pdf, itens):
    for item in itens:
        if pdf.get_y() + 8 > 283:
            pdf.add_page()
        y = pdf.get_y()
        pdf.set_draw_color(235, 232, 226)
        pdf.line(_MARGEM, y + 6.5, 200, y + 6.5)
        pdf.set_font('Helvetica', '', 9.5)
        pdf.set_text_color(40, 40, 40)
        pdf.cell(150, 6.5, _latin1(item['nome']))
        pdf.set_font('Helvetica', 'B', 9.5)
        pdf.set_text_color(146, 100, 33)
        pdf.cell(40, 6.5, _latin1(_moeda(item['preco_venda'])), align='R',
                 new_x='LMARGIN', new_y='NEXT')


def gerar_cardapio_pdf(tipo, categorias, regras):
    """PDF pronto (bytes). `categorias`/`regras` na MESMA forma da tela
    (main._cardapio_categorias — fonte única, nunca divergir da web)."""
    titulo = _TITULO_TIPO.get(tipo, tipo.title())
    pdf = _CardapioPDF(titulo)
    pdf.add_page()
    _capa(pdf, titulo, regras if tipo == 'atacado' else [])

    # Alfabética, com 'Outros' sempre por último (padrão de cardápio).
    for cat in sorted(categorias, key=lambda c: (c == 'Outros', c)):
        itens = categorias[cat]
        com_foto = [i for i in itens
                    if i.get('img_ref') or i.get('imagem_url')]
        sem_foto = [i for i in itens if i not in com_foto]
        _titulo_categoria(pdf, cat,
                          alt_primeira=_CARD_H if com_foto else 10)
        if com_foto:
            _grid_categoria(pdf, com_foto)
        if sem_foto:
            _linhas_sem_foto(pdf, sem_foto)
            pdf.ln(2)

    saida = pdf.output()
    return bytes(saida)

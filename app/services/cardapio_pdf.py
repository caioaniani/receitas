"""Cardápio em PDF gerado no SERVIDOR (19/07/2026, pedido do dono).

O botão "Imprimir" do /cardapio usava window.print() e o navegador
re-paginava o site de qualquer jeito (cards cortados no meio da página,
URL/data no rodapé — fotos do dono, 19/07). Mesma regra da impressão de
pedidos (CLAUDE.md): a saída oficial é PDF do servidor, com paginação
controlada — um card NUNCA é cortado entre páginas.

Layout (A4 retrato) espelha o SITE (main/cardapio.html, pedido do dono
19/07/2026): capa com o hero escuro da marca + regras do pedido (atacado) +
MESMA regra da tela — categoria com ALGUMA foto vira grid de 3 colunas com
TODOS os itens (sem foto = placeholder bege); categoria sem foto nenhuma
vira caixinhas nome/preço em 2 colunas. Fotos: bytes do BLOB
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
_CARD_H_DESC = _FOTO_H + 23            # + até 2 linhas de descrição (7pt)
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
        # Fundo creme da página inteira — o site é #fafaf7, não branco.
        self.set_fill_color(*_C_BG)
        self.rect(0, 0, 210, 297, style='F')
        if self.page_no() == 1:
            return                      # capa desenha o próprio cabeçalho
        self.set_font('Times', 'B', 11)
        self.set_text_color(*_C_FG)
        # '·' e não '—': em-dash está fora do latin-1 e virava '?'.
        self.cell(95, 6, _latin1('O Pão · Padaria Artesanal'))
        self.set_font('Helvetica', '', 9)
        self.set_text_color(*_C_SOFT)
        self.cell(85, 6, _latin1('Cardápio · %s' % self.titulo_tipo),
                  align='R', new_x='LMARGIN', new_y='NEXT')
        self.set_draw_color(*_C_BORDER)
        self.line(_MARGEM, self.get_y() + 1, 200, self.get_y() + 1)
        self.ln(5)

    def footer(self):
        # Rodapé: "O Pão Padaria Artesanal · Brooklin, São Paulo" (o ©
        # foi tirado a pedido do dono 20/07).
        self.set_y(-12)
        self.set_font('Helvetica', '', 8)
        self.set_text_color(*_C_SOFT)
        self.cell(140, 6, _latin1('O Pão Padaria Artesanal · '
                                   'Brooklin, São Paulo'))
        self.cell(40, 6, _latin1('página %d' % self.page_no()), align='R')


def _logo_bytes(logo_data):
    """Bytes da imagem do logo a partir do data URI (AppConfig
    `cardapio_logo_data`). None se ausente/invalido — nunca levanta (o
    cardapio nunca deixa de gerar por causa do logo)."""
    if not logo_data or 'base64,' not in logo_data:
        return None
    try:
        import base64
        return base64.b64decode(logo_data.split('base64,', 1)[1])
    except Exception:  # noqa: BLE001
        logger.warning('cardapio_pdf: logo data URI invalido', exc_info=True)
        return None


def _capa(pdf, titulo_tipo, regras, logo_data=None, preparo=None):
    """Hero escuro da marca (como o do site) + label de seção centrado +
    (atacado) caixa de regras bege + caixa "Métodos de preparo" — espelho
    do main/cardapio.html.

    Com logo configurado (`cardapio_logo_data`), a imagem entra no lugar do
    wordmark "O Pão" na banda escura; sem logo, cai no texto Times."""
    pdf.set_fill_color(*_C_FG)             # hero: o escuro da marca do site
    pdf.rect(0, 0, 210, 58, style='F')
    pdf.set_y(14)
    pdf.set_text_color(215, 210, 202)
    pdf.set_font('Helvetica', '', 9)
    pdf.set_char_spacing(1.6)
    pdf.cell(0, 5, _latin1('PADARIA ARTESANAL · BROOKLIN'),
             new_x='LMARGIN', new_y='NEXT')
    pdf.set_char_spacing(0)
    logo = _logo_bytes(logo_data)
    if logo:
        try:
            # h=26: o logo PREENCHE a faixa escura (era 15mm, pequeno demais
            # — feedback do dono 20/07). Largura auto pela proporcao; wordmark
            # a 26mm fica ~78mm, folgado nos 190mm uteis.
            y_logo = pdf.get_y() + 2
            pdf.image(BytesIO(logo), x=_MARGEM, y=y_logo, h=26)
            pdf.set_y(y_logo + 28)
        except Exception:  # noqa: BLE001 — logo ruim nao derruba o PDF
            logger.warning('cardapio_pdf: logo nao embutiu', exc_info=True)
            logo = None
    if not logo:
        pdf.set_text_color(255, 255, 255)
        pdf.set_font('Times', 'B', 30)
        pdf.cell(0, 14, _latin1('O Pão'), new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 10)
    pdf.set_text_color(220, 216, 210)
    pdf.cell(0, 6, _latin1('Tempo. Fermento. Cuidado. '
                           'Pão de verdade, feito com fermentação natural.'),
             new_x='LMARGIN', new_y='NEXT')
    # Label de seção CENTRADO, como o "CARDÁPIO" da tela (section-label).
    pdf.set_y(66)
    pdf.set_text_color(*_C_SOFT)
    pdf.set_font('Helvetica', 'B', 9)
    pdf.set_char_spacing(2.2)
    pdf.cell(0, 5, _latin1('CARDÁPIO · %s' % titulo_tipo.upper()),
             align='C', new_x='LMARGIN', new_y='NEXT')
    pdf.set_char_spacing(0)

    if regras:
        pdf.ln(3)
        y0 = pdf.get_y()
        alt = 9 + 6 * len(regras)
        pdf.set_fill_color(*_C_TAG)        # .regras-atacado do site
        pdf.set_draw_color(*_C_BORDER)
        pdf.rect(_MARGEM, y0, 190, alt, style='FD',
                 round_corners=True, corner_radius=_RAIO)
        pdf.set_y(y0 + 4)
        pdf.set_x(_MARGEM + 5)
        pdf.set_font('Helvetica', 'B', 9)
        pdf.set_text_color(*_C_PRIMARY)
        pdf.set_char_spacing(0.8)
        # '·' e não em-dash (fora do latin-1) — mesma regra do header.
        pdf.cell(0, 5, _latin1('REGRAS DO PEDIDO · %s'
                               % titulo_tipo.upper()),
                 new_x='LMARGIN', new_y='NEXT')
        pdf.set_char_spacing(0)
        for rg in regras:
            pdf.set_x(_MARGEM + 5)
            pdf.set_font('Helvetica', 'B', 9)
            pdf.set_text_color(*_C_MUTED)
            pdf.cell(pdf.get_string_width(_latin1(rg['label'] + ': ')) + 1, 6,
                     _latin1(rg['label'] + ':'))
            pdf.set_font('Helvetica', '', 9)
            pdf.set_text_color(*_C_FG)
            pdf.cell(0, 6, _latin1(rg['valor']), new_x='LMARGIN', new_y='NEXT')
        pdf.set_y(y0 + alt + 5)
    else:
        pdf.ln(4)


def _moeda(v):
    s = f'{float(v):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    return 'R$ ' + s


def _quebrar_2_linhas(pdf, txt, largura):
    """Quebra `txt` (já _latin1) em até 2 linhas que cabem em `largura`,
    com '...' se sobrar texto. Usa a fonte ATUAL do pdf pra medir."""
    palavras = txt.split()
    linhas, atual = [], ''
    for p in palavras:
        cand = (atual + ' ' + p).strip()
        if pdf.get_string_width(cand) <= largura:
            atual = cand
            continue
        linhas.append(atual)
        atual = p
        if len(linhas) == 2:
            break
    if atual and len(linhas) < 2:
        linhas.append(atual)
    sobrou = len(' '.join(linhas)) < len(txt)
    if sobrou and linhas:
        ult = linhas[-1]
        while pdf.get_string_width(ult + '...') > largura and len(ult) > 3:
            ult = ult[:-1]
        linhas[-1] = ult + '...'
    return linhas


def _box_preparo(pdf, metodos):
    """Caixa "Métodos de preparo" da capa (atacado) — mesma cara bege da
    caixa de regras, com texto LONGO que quebra linha (o backup tem ~3
    linhas). Altura medida antes (dry_run) pra caixa fechar certinho."""
    _LARG_TXT = 180
    _LH = 4.6
    pdf.set_font('Helvetica', 'B', 9)      # B mede mais largo: nunca corta
    alt = 10
    corpos = []
    for m in metodos:
        txt = _latin1((m['label'] + ': ' if m['label'] else '') + m['valor'])
        alt += pdf.multi_cell(_LARG_TXT, _LH, txt,
                              dry_run=True, output='HEIGHT') + 1.6
        corpos.append(txt)
    alt += 1.5
    y0 = pdf.get_y()
    if y0 + alt > _Y_LIMITE:               # capa cheia: caixa em página nova
        pdf.add_page()
        y0 = pdf.get_y()
    pdf.set_fill_color(*_C_TAG)
    pdf.set_draw_color(*_C_BORDER)
    pdf.rect(_MARGEM, y0, 190, alt, style='FD',
             round_corners=True, corner_radius=_RAIO)
    pdf.set_y(y0 + 4)
    pdf.set_x(_MARGEM + 5)
    pdf.set_font('Helvetica', 'B', 9)
    pdf.set_text_color(*_C_PRIMARY)
    pdf.set_char_spacing(0.8)
    pdf.cell(0, 5, _latin1('MÉTODOS DE PREPARO'),
             new_x='LMARGIN', new_y='NEXT')
    pdf.set_char_spacing(0)
    pdf.ln(0.5)
    lm_orig, rm_orig = pdf.l_margin, pdf.r_margin
    pdf.set_left_margin(_MARGEM + 5)
    pdf.set_right_margin(210 - (_MARGEM + 5) - _LARG_TXT)
    for m in metodos:
        pdf.set_x(_MARGEM + 5)
        if m['label']:
            pdf.set_font('Helvetica', 'B', 9)
            pdf.set_text_color(*_C_MUTED)
            pdf.write(_LH, _latin1(m['label'] + ': '))
        pdf.set_font('Helvetica', '', 9)
        pdf.set_text_color(*_C_FG)
        pdf.write(_LH, _latin1(m['valor']))
        pdf.ln(_LH + 1.6)
    pdf.set_left_margin(lm_orig)
    pdf.set_right_margin(rm_orig)
    pdf.set_y(y0 + alt + 5)


def _titulo_categoria(pdf, nome, alt_primeira=_CARD_H):
    # Categoria órfã no pé da página: quebra antes (nunca título solto).
    # `alt_primeira` = altura do 1º bloco da categoria — card de foto no
    # grid, ou só ~10mm quando a seção é de linhas de texto (senão 3
    # linhas ganhavam página própria à toa).
    if pdf.get_y() + 14 + alt_primeira > 281:
        pdf.add_page()
    pdf.ln(2)
    # .cat-heading do site: sans bold, cor fg, sem sublinhado.
    pdf.set_font('Helvetica', 'B', 13.5)
    pdf.set_text_color(*_C_FG)
    pdf.cell(0, 8, _latin1(nome), new_x='LMARGIN', new_y='NEXT')
    pdf.ln(2.5)


def _card(pdf, x, y, item, foto, card_h=_CARD_H):
    """.product-card do site: branco, borda #e8e3d7, cantos arredondados,
    foto QUADRADA no topo (ou placeholder bege), nome fg + preço marrom.
    `card_h=_CARD_H_DESC` quando a CATEGORIA tem descrições (altura uniforme
    na fileira; item sem descrição só fica com o respiro)."""
    pdf.set_draw_color(*_C_BORDER)
    pdf.set_fill_color(255, 255, 255)
    pdf.rect(x, y, _COL_W, card_h, style='FD',
             round_corners=True, corner_radius=_RAIO)
    if foto:
        pdf.image(BytesIO(foto), x=x + 0.5, y=y + 0.5,
                  w=_COL_W - 1, h=_FOTO_H - 1)
    else:
        # .card-img.placeholder: bege com a marca discreta no centro.
        pdf.set_fill_color(*_C_TAG)
        pdf.rect(x + 0.5, y + 0.5, _COL_W - 1, _FOTO_H - 1, style='F',
                 round_corners=True, corner_radius=_RAIO)
        pdf.set_font('Times', 'I', 13)
        pdf.set_text_color(*_C_SOFT)
        pdf.set_xy(x, y + _FOTO_H / 2 - 3)
        pdf.cell(_COL_W, 6, _latin1('O Pão'), align='C')
    pdf.set_xy(x + 2.5, y + _FOTO_H + 1.5)
    pdf.set_font('Helvetica', 'B', 8.5)
    pdf.set_text_color(*_C_FG)
    nome = _latin1(item['nome'])
    if pdf.get_string_width(nome) > _COL_W - 5:
        while pdf.get_string_width(nome + '...') > _COL_W - 5 and len(nome) > 3:
            nome = nome[:-1]
        nome += '...'
    pdf.cell(_COL_W - 5, 4.5, nome)
    if card_h > _CARD_H:
        desc = item.get('descricao')
        if desc:
            pdf.set_font('Helvetica', '', 7)
            pdf.set_text_color(*_C_MUTED)
            for i, ln in enumerate(
                    _quebrar_2_linhas(pdf, _latin1(desc), _COL_W - 5)):
                pdf.set_xy(x + 2.5, y + _FOTO_H + 6.2 + i * 3.1)
                pdf.cell(_COL_W - 5, 3.1, ln)
        y_preco = y + _FOTO_H + 13.4
    else:
        y_preco = y + _FOTO_H + 6.5
    pdf.set_xy(x + 2.5, y_preco)
    pdf.set_font('Helvetica', 'B', 9.5)
    pdf.set_text_color(*_C_PRIMARY)
    pdf.cell(_COL_W - 5, 5, _latin1(_moeda(item['preco_venda'])))


def _grid_categoria(pdf, itens_foto, card_h=_CARD_H):
    # LINHA a linha com y0 CONGELADO: os cells de texto do card movem o
    # cursor do fpdf2 — usar get_y() por card fazia os 3 da linha descerem
    # em cascata (pego na inspeção visual da 1ª amostra, 19/07/2026).
    for i in range(0, len(itens_foto), 3):
        linha = itens_foto[i:i + 3]
        if pdf.get_y() + card_h > 281:
            pdf.add_page()
        y0 = pdf.get_y()
        for col, item in enumerate(linha):
            x = _MARGEM + col * (_COL_W + _GAP)
            _card(pdf, x, y0, item, _bytes_foto(item), card_h=card_h)
        pdf.set_y(y0 + card_h + _GAP)


_LINHA_H = 10
_LINHA_H_DESC = 17                      # nome + até 2 linhas de descrição
_LINHA_W = (190 - _GAP) / 2


def _lista_categoria(pdf, itens):
    """.list-grid do site (categoria SEM nenhuma foto): caixinhas brancas
    arredondadas em 2 colunas, nome à esquerda + preço marrom à direita.
    Categoria com alguma descrição: caixinha mais alta com o texto muted
    embaixo (mesma altura pra fileira toda)."""
    tem_desc = any(i.get('descricao') for i in itens)
    lh = _LINHA_H_DESC if tem_desc else _LINHA_H
    for i in range(0, len(itens), 2):
        linha = itens[i:i + 2]
        if pdf.get_y() + lh > 283:
            pdf.add_page()
        y0 = pdf.get_y()
        for col, item in enumerate(linha):
            x = _MARGEM + col * (_LINHA_W + _GAP)
            pdf.set_draw_color(*_C_BORDER)
            pdf.set_fill_color(255, 255, 255)
            pdf.rect(x, y0, _LINHA_W, lh, style='FD',
                     round_corners=True, corner_radius=_RAIO)
            preco = _latin1(_moeda(item['preco_venda']))
            pdf.set_font('Helvetica', 'B', 9)
            w_preco = pdf.get_string_width(preco) + 2
            pdf.set_xy(x + 4, y0 + 2)
            pdf.set_text_color(*_C_FG)
            nome = _latin1(item['nome'])
            max_nome = _LINHA_W - 8 - w_preco
            if pdf.get_string_width(nome) > max_nome:
                while (pdf.get_string_width(nome + '...') > max_nome
                       and len(nome) > 3):
                    nome = nome[:-1]
                nome += '...'
            pdf.cell(max_nome, 6, nome)
            pdf.set_xy(x + _LINHA_W - w_preco - 4, y0 + 2)
            pdf.set_text_color(*_C_PRIMARY)
            pdf.cell(w_preco, 6, preco, align='R')
            if tem_desc and item.get('descricao'):
                pdf.set_font('Helvetica', '', 7)
                pdf.set_text_color(*_C_MUTED)
                for j, ln in enumerate(_quebrar_2_linhas(
                        pdf, _latin1(item['descricao']), _LINHA_W - 8)):
                    pdf.set_xy(x + 4, y0 + 8.2 + j * 3.1)
                    pdf.cell(_LINHA_W - 8, 3.1, ln)
        pdf.set_y(y0 + lh + 2)


# Limite inferior da area de conteudo (auto_page_break margem 16 -> ~281) e
# altura util de uma pagina LIMPA (topo apos o header ~30mm). Usados pra
# manter uma categoria INTEIRA numa pagina (feedback do dono 20/07: a
# categoria "Paes" quebrava entre paginas).
_Y_LIMITE = 281
_PAG_UTIL = 250


def _altura_categoria(itens, tem_foto):
    """Estimativa da altura (mm) da categoria: titulo + fileiras. Grid = 3
    por fileira (card alto); lista = 2 por fileira (caixinha baixa).
    Categoria com alguma descrição usa a fileira mais alta correspondente."""
    import math
    titulo = 12.5
    tem_desc = any(i.get('descricao') for i in itens)
    if tem_foto:
        card_h = _CARD_H_DESC if tem_desc else _CARD_H
        fileiras = math.ceil(len(itens) / 3)
        return titulo + fileiras * (card_h + _GAP)
    lh = _LINHA_H_DESC if tem_desc else _LINHA_H
    fileiras = math.ceil(len(itens) / 2)
    return titulo + fileiras * (lh + 2) + 2


def gerar_cardapio_pdf(tipo, categorias, regras, logo=None):
    """PDF pronto (bytes). `categorias`/`regras` na MESMA forma da tela
    (main._cardapio_categorias — fonte única, nunca divergir da web).
    `logo`: data URI do logotipo (AppConfig) ou None (cai no texto "O Pão")."""
    titulo = _TITULO_TIPO.get(tipo, tipo.title())
    pdf = _CardapioPDF(titulo)
    pdf.add_page()
    _capa(pdf, titulo, regras if tipo == 'atacado' else [], logo_data=logo)

    # Alfabética, com 'Outros' sempre por último (padrão de cardápio).
    for cat in sorted(categorias, key=lambda c: (c == 'Outros', c)):
        itens = categorias[cat]
        # MESMA regra do site (main/cardapio.html `tem_foto`): categoria com
        # ALGUMA foto vira grid de cards com TODOS os itens (sem foto =
        # placeholder bege); categoria sem foto nenhuma vira as caixinhas
        # nome/preço em 2 colunas.
        tem_foto = any(i.get('img_ref') or i.get('imagem_url')
                       for i in itens)
        # MANTER A CATEGORIA INTEIRA NUMA PAGINA (20/07/2026): se nao cabe no
        # espaco que sobrou mas cabe numa pagina limpa, comeca numa nova. Se
        # for maior que uma pagina inteira, flui e quebra por fileira (o
        # _grid/_lista ja tratam). Efeito: pagina 1 vira a CAPA, cada
        # categoria comeca limpa — sem "Paes" partido no meio.
        alt = _altura_categoria(itens, tem_foto)
        if alt > (_Y_LIMITE - pdf.get_y()) and alt <= _PAG_UTIL:
            pdf.add_page()
        _titulo_categoria(pdf, cat,
                          alt_primeira=_CARD_H if tem_foto else _LINHA_H)
        if tem_foto:
            _grid_categoria(pdf, itens)
        else:
            _lista_categoria(pdf, itens)
            pdf.ln(2)

    saida = pdf.output()
    return bytes(saida)

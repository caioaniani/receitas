"""Boleto Sicredi (banco 748) — ficha de compensação em PDF (06/07/2026).

Fase 2 da homologação: o banco validou a remessa e pediu "novo arquivo
remessa E BOLETOS". Este módulo monta o código de barras e a linha
digitável conforme o manual oficial (COBManualCNAB400 §10) e desenha o
boleto (recibo do pagador + ficha de compensação) com fpdf2.

Regras do manual:
- Código de barras (44): 748 + 9 + DV geral + fator vencimento(4) +
  valor(10) + campo livre(25)                                   (§10.2)
- Campo livre (25): tipo cobrança '1' + carteira '1' + nosso número(9) +
  cooperativa(4) + posto(2) + beneficiário(5) + '1' (valor expresso) +
  '0' (filler) + DV módulo 11                                   (§10.3)
- DV geral: módulo 11 sobre os 43 dígitos; 11-resto > 9 → DV 1  (§10.5)
- Fator de vencimento: dias desde 07/10/1997; ao passar de 9999
  (21/02/2025) volta pra 1000 em 22/02/2025                     (§10.7)
- Linha digitável: 5 campos, DVs por módulo 10                  (§10.8)
- Barra "2 de 5 intercalado", 103mm x 13mm, início a 0,5cm da margem
  esquerda, meio da barra a 12mm do final da folha              (§10.6)

Fixture de validação (boleto-modelo oficial do banco): linha digitável
74891.12115 03527.501013 19002.071041 6 85810000018000 — travada em
tests/test_cobrancas_sicredi.py.
"""
import os
from datetime import date

from fpdf import FPDF

from app.services.pdf import _latin1
from app.services.sicredi_cnab import _ascii, _centavos, _cfg, _num

_BASE_FATOR = date(1997, 10, 7)


# ── matemática do manual ────────────────────────────────────────────────────

def _mod11(base, para_codigo_barras=False):
    """§10.10: pesos 2..9 da direita pra esquerda; DV = 11 - resto.
    Resultado > 9 → 0; no código de barras, 0 vira 1 (DV geral nunca é 0)."""
    soma, peso = 0, 2
    for ch in reversed(base):
        soma += int(ch) * peso
        peso = 2 if peso == 9 else peso + 1
    dv = 11 - (soma % 11)
    if dv > 9:
        dv = 0
    if para_codigo_barras and dv == 0:
        dv = 1
    return dv


def _mod10(base):
    """§10.8.3: pesos 2,1,2,1... da direita pra esquerda; produto com dois
    dígitos soma os dígitos; DV = múltiplo de 10 seguinte - soma."""
    soma, peso = 0, 2
    for ch in reversed(base):
        p = int(ch) * peso
        soma += p if p < 10 else p - 9
        peso = 1 if peso == 2 else 2
    return (10 - soma % 10) % 10


def fator_vencimento(venc):
    """§10.7: dias desde 07/10/1997. Chegou em 9999 (21/02/2025), recomeça
    em 1000 no dia seguinte (Comunicado FEBRABAN FB-082/FB-122)."""
    f = (venc - _BASE_FATOR).days
    while f > 9999:
        f -= 9000
    if f < 1000:
        raise ValueError(f'Fator de vencimento inválido pra {venc}.')
    return f


def campo_livre(nosso_numero, agencia, posto, beneficiario,
                valor_expresso=True):
    """§10.3 — 25 posições. Posto não numérico entra como '00' (mesma
    convenção do DV do nosso número)."""
    posto = posto if str(posto).isdigit() else '00'
    base = ('1'                                  # cobrança com registro
            + '1'                                # carteira simples
            + _num(nosso_numero, 9)
            + _num(agencia, 4)
            + _num(posto, 2)
            + _num(beneficiario, 5)
            + ('1' if valor_expresso else '0')   # valor expresso na barra
            + '0')                               # filler
    return base + str(_mod11(base))


def montar_codigo_barras(nosso_numero, valor, vencimento, cfg=None):
    """§10.2 — 44 posições, com o DV geral na 5ª."""
    c = cfg or _cfg()
    centavos = _centavos(valor)
    cl = campo_livre(nosso_numero, c['agencia'], c['posto'],
                     c['beneficiario'], valor_expresso=centavos > 0)
    fator = f'{fator_vencimento(vencimento):04d}'
    sem_dv = '7489' + fator + _num(centavos, 10) + cl
    dv = _mod11(sem_dv, para_codigo_barras=True)
    return sem_dv[:4] + str(dv) + sem_dv[4:]


def linha_digitavel(codigo_barras):
    """§10.8 — 5 campos com DVs módulo 10 nos três primeiros."""
    cl = codigo_barras[19:44]
    c1 = codigo_barras[0:4] + cl[0:5]
    c1 += str(_mod10(c1))
    c2 = cl[5:15]
    c2 += str(_mod10(c2))
    c3 = cl[15:25]
    c3 += str(_mod10(c3))
    c4 = codigo_barras[4]
    c5 = codigo_barras[5:19]
    return (f'{c1[:5]}.{c1[5:]} {c2[:5]}.{c2[5:]} {c3[:5]}.{c3[5:]} '
            f'{c4} {c5}')


def codigo_barras_da_cobranca(cob):
    if not cob.nosso_numero:
        raise ValueError('Cobrança sem nosso número — gere a remessa antes.')
    return montar_codigo_barras(cob.nosso_numero, cob.valor, cob.vencimento)


# ── desenho ─────────────────────────────────────────────────────────────────

# ITF "2 de 5 intercalado" (§10.6): 5 barras por dígito, 2 largas; os
# espaços também codificam (dígito das barras intercalado com o dos espaços).
_ITF = {
    '0': 'nnwwn', '1': 'wnnnw', '2': 'nwnnw', '3': 'wwnnn', '4': 'nnwnw',
    '5': 'wnwnn', '6': 'nwwnn', '7': 'nnnww', '8': 'wnnwn', '9': 'nwnwn',
}


def _desenhar_itf(pdf, codigo, x, y, largura=103.0, altura=13.0):
    """Desenha o código "2 de 5 intercalado" com 103mm x 13mm (§10.6)."""
    assert len(codigo) % 2 == 0 and codigo.isdigit()
    elementos = [(True, False), (False, False),
                 (True, False), (False, False)]          # start: nnnn
    for i in range(0, len(codigo), 2):
        barras, espacos = _ITF[codigo[i]], _ITF[codigo[i + 1]]
        for b, e in zip(barras, espacos):
            elementos.append((True, b == 'w'))
            elementos.append((False, e == 'w'))
    elementos += [(True, True), (False, False), (True, False)]  # stop: wnn
    razao = 3.0
    unidades = sum(razao if largo else 1 for _, largo in elementos)
    modulo = largura / unidades
    pdf.set_fill_color(0, 0, 0)
    cx = x
    for e_barra, largo in elementos:
        w = modulo * (razao if largo else 1)
        if e_barra:
            pdf.rect(cx, y, w, altura, 'F')
        cx += w


def _desenhar_qr(pdf, texto, x, y, tamanho=25.0):
    import qrcode
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                       border=0)
    qr.add_data(texto)
    qr.make(fit=True)
    matriz = qr.get_matrix()
    modulo = tamanho / len(matriz)
    pdf.set_fill_color(0, 0, 0)
    for i, linha in enumerate(matriz):
        for j, cheio in enumerate(linha):
            if cheio:
                pdf.rect(x + j * modulo, y + i * modulo, modulo, modulo, 'F')


def _fmt_moeda(v):
    s = f'{float(v):,.2f}'
    return s.replace(',', '_').replace('.', ',').replace('_', '.')


def _fmt_doc(doc):
    d = ''.join(ch for ch in (doc or '') if ch.isdigit())
    if len(d) == 14:
        return f'{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}'
    if len(d) == 11:
        return f'{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}'
    return doc or ''


def _fmt_cep(cep):
    d = ''.join(ch for ch in (cep or '') if ch.isdigit())
    return f'{d[:5]}-{d[5:]}' if len(d) == 8 else (cep or '')


def _campo(pdf, x, y, w, h, label, valor='', *, bold=True, alinhar='L',
           tam=8):
    """Uma caixa do boleto: label pequeno em cima, valor embaixo."""
    pdf.rect(x, y, w, h)
    pdf.set_xy(x + 0.8, y + 0.4)
    pdf.set_font('Helvetica', '', 5.2)
    pdf.cell(w - 1.6, 2.4, _latin1(label))
    pdf.set_xy(x + 0.8, y + 3.0)
    pdf.set_font('Helvetica', 'B' if bold else '', tam)
    pdf.cell(w - 1.6, h - 3.6, _latin1(valor), align=alinhar)


def _header_banco(pdf, x, y, w, linha_dig):
    """SICREDI | 748-X | linha digitável, com o filete grosso embaixo."""
    pdf.set_xy(x, y)
    pdf.set_font('Helvetica', 'B', 13)
    pdf.cell(38, 8, 'SICREDI', align='L')
    pdf.set_line_width(0.6)
    pdf.line(x + 38, y + 0.5, x + 38, y + 8)
    pdf.set_font('Helvetica', 'B', 14)
    pdf.set_xy(x + 38, y)
    pdf.cell(18, 8, '748-X', align='C')
    pdf.line(x + 56, y + 0.5, x + 56, y + 8)
    pdf.set_font('Helvetica', 'B', 10.4)
    pdf.set_xy(x + 56, y)
    pdf.cell(w - 56, 8, linha_dig, align='R')
    pdf.line(x, y + 8, x + w, y + 8)
    pdf.set_line_width(0.2)


def _bloco_pagador(pdf, x, y, w, h, cob):
    pdf.rect(x, y, w, h)
    pdf.set_xy(x + 0.8, y + 0.4)
    pdf.set_font('Helvetica', '', 5.2)
    pdf.cell(20, 2.4, 'Pagador')
    pdf.set_xy(x + 0.8, y + 3.2)
    pdf.set_font('Helvetica', 'B', 8.5)
    pdf.cell(w - 1.6, 3.6, _latin1(
        f'{cob.pagador_nome}  -  CNPJ/CPF: {_fmt_doc(cob.pagador_cnpj_cpf)}'))
    pdf.set_xy(x + 0.8, y + 7.4)
    pdf.set_font('Helvetica', '', 8)
    pdf.cell(w - 1.6, 3.4, _latin1(cob.pagador_endereco or ''))
    pdf.set_xy(x + 0.8, y + 11.0)
    pdf.cell(w - 1.6, 3.4, _latin1(f'CEP: {_fmt_cep(cob.pagador_cep)}'))


def _dados(cob):
    c = _cfg()
    benef_nome = os.environ.get('SICREDI_BENEF_NOME',
                                'O PAO PADARIA ARTESANAL LTDA')
    return {
        'cfg': c,
        'benef': f'{benef_nome}  -  CNPJ: {_fmt_doc(c["cnpj"])}',
        'ag_cod': f'{c["agencia"]}.{c["posto"]}.{c["beneficiario"]}',
        'venc': cob.vencimento.strftime('%d/%m/%Y'),
        'emissao': cob.emissao.strftime('%d/%m/%Y'),
        'valor': _fmt_moeda(cob.valor),
        'nosso': cob.nosso_numero_fmt,
    }


def gerar_boleto_pdf(cob):
    """PDF A4: recibo do pagador no topo + ficha de compensação na base da
    folha (o manual fixa o meio da barra a 12mm do final da folha)."""
    cb = codigo_barras_da_cobranca(cob)
    ld = linha_digitavel(cb)
    d = _dados(cob)

    pdf = FPDF(format='A4')
    pdf.set_auto_page_break(False)
    pdf.set_margins(8, 8)
    pdf.add_page()
    pdf.set_line_width(0.2)
    pdf.set_draw_color(0, 0, 0)
    x, w = 8.0, 194.0

    # ── Recibo do pagador ──
    y = 12.0
    pdf.set_xy(x, y - 4)
    pdf.set_font('Helvetica', '', 6.5)
    pdf.cell(w, 3, 'RECIBO DO PAGADOR', align='R')
    _header_banco(pdf, x, y, w, ld)
    y += 8
    _campo(pdf, x, y, 144, 8, 'Beneficiário', d['benef'], tam=8.5)
    _campo(pdf, x + 144, y, 50, 8, 'Agência/Código Beneficiário',
           d['ag_cod'], alinhar='R')
    y += 8
    _campo(pdf, x, y, 144, 8, 'Pagador', cob.pagador_nome, bold=False,
           tam=8.5)
    _campo(pdf, x + 144, y, 50, 8, 'Vencimento', d['venc'], alinhar='R')
    y += 8
    _campo(pdf, x, y, 48, 8, 'Nosso número', d['nosso'], bold=False)
    _campo(pdf, x + 48, y, 48, 8, 'Nº do documento', cob.seu_numero,
           bold=False)
    _campo(pdf, x + 96, y, 48, 8, 'Data do documento', d['emissao'],
           bold=False)
    _campo(pdf, x + 144, y, 50, 8, '(=) Valor do documento', d['valor'],
           alinhar='R')
    y += 8
    pdf.set_xy(x, y + 1)
    pdf.set_font('Helvetica', '', 6)
    pdf.cell(w, 3, 'Autenticação mecânica', align='R')

    # ── corte ──
    y_corte = y + 12
    pdf.set_xy(x, y_corte - 3.2)
    pdf.set_font('Helvetica', '', 6)
    pdf.cell(w, 3, 'Corte na linha pontilhada', align='R')
    pdf.dashed_line(x, y_corte, x + w, y_corte, 2, 1.6)

    # ── Ficha de compensação (na base da folha, §10.6) ──
    y_barra = 278.5                       # meio da barra = 285 = 297 - 12
    y = 170.0
    _header_banco(pdf, x, y, w, ld)
    y += 8
    _campo(pdf, x, y, 144, 8, 'Local de pagamento',
           'PAGÁVEL PREFERENCIALMENTE NAS COOPERATIVAS DE CRÉDITO DO '
           'SICREDI', bold=False, tam=7.5)
    _campo(pdf, x + 144, y, 50, 8, 'Vencimento', d['venc'], alinhar='R',
           tam=9)
    y += 8
    _campo(pdf, x, y, 144, 8, 'Beneficiário', d['benef'], tam=8.5)
    _campo(pdf, x + 144, y, 50, 8, 'Agência/Código Beneficiário',
           d['ag_cod'], alinhar='R')
    y += 8
    _campo(pdf, x, y, 26, 8, 'Data do documento', d['emissao'], bold=False)
    _campo(pdf, x + 26, y, 44, 8, 'Nº do documento', cob.seu_numero,
           bold=False)
    _campo(pdf, x + 70, y, 22, 8, 'Espécie doc.', 'DMI', bold=False)
    _campo(pdf, x + 92, y, 14, 8, 'Aceite', 'N', bold=False)
    _campo(pdf, x + 106, y, 38, 8, 'Data processamento', d['emissao'],
           bold=False)
    _campo(pdf, x + 144, y, 50, 8, 'Nosso número', d['nosso'], alinhar='R')
    y += 8
    _campo(pdf, x, y, 26, 8, 'Uso do banco', '', bold=False)
    _campo(pdf, x + 26, y, 22, 8, 'Carteira', '1', bold=False)
    _campo(pdf, x + 48, y, 22, 8, 'Espécie', 'R$', bold=False)
    _campo(pdf, x + 70, y, 36, 8, 'Quantidade', '', bold=False)
    _campo(pdf, x + 106, y, 38, 8, '(x) Valor', '', bold=False)
    _campo(pdf, x + 144, y, 50, 8, '(=) Valor do documento', d['valor'],
           alinhar='R', tam=9)
    y += 8

    # Instruções (com QR Pix do híbrido, quando o retorno já o trouxe)
    alt_instr = 28.0
    pdf.rect(x, y, 144, alt_instr)
    pdf.set_xy(x + 0.8, y + 0.4)
    pdf.set_font('Helvetica', '', 5.2)
    pdf.cell(80, 2.4, 'Instruções (texto de responsabilidade do '
                      'beneficiário)')
    pdf.set_xy(x + 0.8, y + 3.6)
    pdf.set_font('Helvetica', '', 8)
    instrucoes = [f'Referente a {cob.seu_numero}.']
    if cob.pix_copia_cola:
        instrucoes.append('Boleto híbrido: pague pelo código de barras ou '
                          'pelo QR Code Pix ao lado.')
    for i, txt in enumerate(instrucoes):
        pdf.set_xy(x + 0.8, y + 3.6 + i * 4.2)
        pdf.cell(115, 3.8, _latin1(txt))
    if cob.pix_copia_cola:
        _desenhar_qr(pdf, cob.pix_copia_cola, x + 117, y + 1.5,
                     tamanho=alt_instr - 3)
    for i, (rot, val) in enumerate([('(-) Desconto / Abatimento', ''),
                                    ('(+) Mora / Multa', ''),
                                    ('(=) Valor cobrado', '')]):
        _campo(pdf, x + 144, y + i * (alt_instr / 3), 50, alt_instr / 3,
               rot, val, bold=False, alinhar='R')
    y += alt_instr

    _bloco_pagador(pdf, x, y, w, 15, cob)
    y += 15
    _campo(pdf, x, y, w, 7, 'Sacador/Avalista', '', bold=False)
    y += 7
    pdf.set_xy(x, y + 0.6)
    pdf.set_font('Helvetica', '', 6)
    pdf.cell(w, 3, 'Autenticação mecânica - FICHA DE COMPENSAÇÃO',
             align='R')

    # Barra: início a 0,5cm da margem esquerda da folha (§10.6)
    _desenhar_itf(pdf, cb, x=5.0, y=y_barra)
    return pdf.output()


def nome_arquivo_boleto(cob):
    ref = _ascii(cob.pagador_nome, 20).strip().replace(' ', '_') or 'boleto'
    return f'boleto_{cob.nosso_numero}_{ref}.pdf'

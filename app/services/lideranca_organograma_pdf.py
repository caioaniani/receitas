"""PDF horizontal do organograma da equipe.

O desenho e feito diretamente com fpdf2 para nao depender do motor HTML do
servidor. Cada nivel avanca da esquerda para a direita e os conectores deixam
claro quem responde a quem.
"""
from fpdf import FPDF


def _texto(valor):
    substituicoes = (
        ('—', '-'), ('–', '-'), ('“', '"'), ('”', '"'),
        ('‘', "'"), ('’', "'"), ('…', '...'),
    )
    valor = str(valor or '')
    for antigo, novo in substituicoes:
        valor = valor.replace(antigo, novo)
    return valor.encode('latin-1', 'replace').decode('latin-1')


def _cortar(pdf, valor, largura):
    valor = _texto(valor)
    if pdf.get_string_width(valor) <= largura:
        return valor
    sufixo = '...'
    while valor and pdf.get_string_width(valor + sufixo) > largura:
        valor = valor[:-1]
    return valor.rstrip() + sufixo


def _profundidade_maxima(raizes, filhos_por_lider):
    def profundidade(pessoa):
        filhos = filhos_por_lider.get(pessoa.id, [])
        return 0 if not filhos else 1 + max(profundidade(f) for f in filhos)

    return max((profundidade(raiz) for raiz in raizes), default=0)


def gerar_pdf(dados, gerado_em):
    """Retorna o organograma completo em uma pagina A1 paisagem."""
    pdf = FPDF(orientation='L', unit='mm', format=(594, 841))
    pdf.set_auto_page_break(auto=False)
    pdf.set_margins(12, 10, 12)
    pdf.set_title(_texto('Organograma da equipe'))
    pdf.set_author(_texto('O Pão Padaria Artesanal'))
    pdf.set_subject(_texto('Hierarquia, unidade e período da equipe'))
    pdf.add_page()

    margem = 12
    largura_util = pdf.w - 2 * margem
    pdf.set_fill_color(35, 94, 66)
    pdf.rect(0, 0, pdf.w, 4, style='F')
    pdf.set_xy(margem, 11)
    pdf.set_text_color(40, 98, 71)
    pdf.set_font('Helvetica', 'B', 7)
    pdf.cell(0, 4, _texto('O PÃO  ·  PESSOAS'))
    pdf.set_xy(margem, 18)
    pdf.set_text_color(28, 40, 33)
    pdf.set_font('Helvetica', 'B', 23)
    pdf.cell(0, 10, _texto('Organograma da equipe'))
    pdf.set_xy(margem, 30)
    pdf.set_text_color(102, 115, 108)
    pdf.set_font('Helvetica', '', 8)
    pdf.cell(0, 4, _texto('Liderança, unidade principal e período de trabalho'))
    pdf.set_xy(pdf.w - 100, 19)
    pdf.set_font('Helvetica', '', 7)
    pdf.cell(88, 5, _texto(
        f'Gerado em {gerado_em.strftime("%d/%m/%Y às %H:%M")}'), align='R')

    metricas = (
        ('PESSOAS ATIVAS', len(dados['funcionarios'])),
        ('LÍDERES', dados['total_lideres']),
        ('MAIOR EQUIPE DIRETA', dados['maior_equipe']),
        ('CADASTROS INCOMPLETOS', dados['pendencias']),
    )
    largura_metrica = 47
    for indice, (rotulo, valor) in enumerate(metricas):
        x = margem + indice * (largura_metrica + 4)
        y = 39
        pendente = indice == 3 and valor
        pdf.set_fill_color(*(255, 249, 237) if pendente else (249, 252, 250))
        pdf.set_draw_color(*(224, 193, 135) if pendente else (216, 227, 221))
        pdf.rect(x, y, largura_metrica, 17, style='DF')
        pdf.set_xy(x + 3, y + 2.5)
        pdf.set_text_color(102, 115, 108)
        pdf.set_font('Helvetica', 'B', 5.5)
        pdf.cell(largura_metrica - 6, 3, _texto(rotulo))
        pdf.set_xy(x + 3, y + 7)
        pdf.set_text_color(*(138, 91, 18) if pendente else (28, 40, 33))
        pdf.set_font('Helvetica', 'B', 14)
        pdf.cell(largura_metrica - 6, 7, str(valor))

    pdf.set_xy(margem, 64)
    pdf.set_text_color(28, 40, 33)
    pdf.set_font('Helvetica', 'B', 12)
    pdf.cell(0, 6, _texto('Quem responde a quem'))
    pdf.set_xy(margem, 72)
    pdf.set_text_color(102, 115, 108)
    pdf.set_font('Helvetica', '', 6)
    pdf.cell(0, 4, _texto('A leitura começa à esquerda e acompanha os níveis para a direita.'))

    raizes = dados['raizes']
    filhos_por_lider = dados['filhos_por_lider']
    lojas_por_id = dados['lojas_por_id']
    unidades = dados['unidades']
    if not raizes:
        pdf.set_xy(margem, 95)
        pdf.set_font('Helvetica', '', 10)
        pdf.cell(0, 8, _texto('Nenhuma pessoa ativa encontrada.'))
        return bytes(pdf.output())

    topo = 82
    base = pdf.h - 16
    altura_util = base - topo
    profundidade_maxima = _profundidade_maxima(raizes, filhos_por_lider)
    espaco_x = 16
    largura_cartao = min(
        140, (largura_util - profundidade_maxima * espaco_x)
        / (profundidade_maxima + 1))
    largura_cartao = max(53, largura_cartao)
    largura_arvore = ((profundidade_maxima + 1) * largura_cartao
                      + profundidade_maxima * espaco_x)
    inicio_x = margem + max(0, (largura_util - largura_arvore) / 2)

    folhas = {}

    def contar_folhas(pessoa):
        filhos = filhos_por_lider.get(pessoa.id, [])
        folhas[pessoa.id] = sum(contar_folhas(f) for f in filhos) if filhos else 1
        return folhas[pessoa.id]

    total_folhas = sum(contar_folhas(raiz) for raiz in raizes)
    altura_slot = min(17, altura_util / max(total_folhas, 1))
    altura_cartao = max(10.2, min(13, altura_slot * .8))
    inicio_y = topo + max(0, (altura_util - altura_slot * total_folhas) / 2)
    cursor = [0]
    posicoes = {}

    def posicionar(pessoa, nivel):
        filhos = filhos_por_lider.get(pessoa.id, [])
        if filhos:
            centros = [posicionar(filho, nivel + 1) for filho in filhos]
            centro = (centros[0] + centros[-1]) / 2
        else:
            centro = inicio_y + (cursor[0] + .5) * altura_slot
            cursor[0] += 1
        posicoes[pessoa.id] = (
            inicio_x + nivel * (largura_cartao + espaco_x), centro)
        return centro

    for raiz in raizes:
        posicionar(raiz, 0)

    pdf.set_draw_color(185, 203, 192)
    pdf.set_line_width(.45)
    for pessoa in dados['funcionarios']:
        filhos = filhos_por_lider.get(pessoa.id, [])
        if not filhos or pessoa.id not in posicoes:
            continue
        x, y = posicoes[pessoa.id]
        saida_x = x + largura_cartao
        encontro_x = saida_x + espaco_x / 2
        centros = [posicoes[filho.id][1] for filho in filhos]
        pdf.line(saida_x, y, encontro_x, y)
        pdf.line(encontro_x, min(centros), encontro_x, max(centros))
        for filho in filhos:
            filho_x, filho_y = posicoes[filho.id]
            pdf.line(encontro_x, filho_y, filho_x, filho_y)

    def desenhar_cartao(pessoa, nivel):
        x, centro = posicoes[pessoa.id]
        y = centro - altura_cartao / 2
        unidade = lojas_por_id.get(unidades.get(pessoa.id))
        cargo = pessoa.cargo.nome if pessoa.cargo else (pessoa.funcao or 'Sem cargo')
        incompleto = not unidade or not pessoa.periodo
        if nivel == 0:
            preenchimento, borda = (247, 251, 248), (83, 139, 108)
        elif incompleto:
            preenchimento, borda = (255, 250, 240), (226, 192, 131)
        else:
            preenchimento, borda = (255, 255, 255), (216, 227, 221)
        pdf.set_fill_color(*preenchimento)
        pdf.set_draw_color(*borda)
        pdf.set_line_width(.35)
        pdf.rect(x, y, largura_cartao, altura_cartao, style='DF')
        padding = 2.5
        disponivel = largura_cartao - 2 * padding
        pdf.set_xy(x + padding, y + 1.3)
        pdf.set_text_color(28, 40, 33)
        pdf.set_font('Helvetica', 'B', 7.4)
        pdf.cell(disponivel, 3, _cortar(pdf, pessoa.nome, disponivel))
        pdf.set_xy(x + padding, y + 4.5)
        pdf.set_text_color(102, 115, 108)
        pdf.set_font('Helvetica', '', 5.6)
        pdf.cell(disponivel, 2.7, _cortar(pdf, cargo.upper(), disponivel))
        unidade_nome = unidade.nome if unidade else 'Unidade não informada'
        periodo = pessoa.periodo or 'Período não informado'
        meta = f'{unidade_nome}  ·  {periodo}'
        pdf.set_xy(x + padding, y + altura_cartao - 3.8)
        pdf.set_text_color(*(138, 91, 18) if incompleto else (70, 102, 84))
        pdf.set_font('Helvetica', 'B', 5.3)
        pdf.cell(disponivel, 2.6, _cortar(pdf, meta, disponivel))

    def percorrer(pessoa, nivel=0):
        desenhar_cartao(pessoa, nivel)
        for filho in filhos_por_lider.get(pessoa.id, []):
            percorrer(filho, nivel + 1)

    for raiz in raizes:
        percorrer(raiz)

    pdf.set_xy(margem, pdf.h - 9)
    pdf.set_text_color(121, 133, 126)
    pdf.set_font('Helvetica', '', 5)
    pdf.cell(0, 4, _texto('Organograma gerado pelo sistema de gestão O Pão'))
    return bytes(pdf.output())

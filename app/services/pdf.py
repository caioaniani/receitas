from fpdf import FPDF


def _latin1(texto):
    """Sanitiza string pra fonte core do FPDF (latin-1). Acentos do
    portugues (ã, ç, é...) passam intactos; emoji e outros simbolos fora
    do latin-1 viram '?'. Necessario porque a cartinha vem do cliente e
    pode ter qualquer coisa."""
    if texto is None:
        return ''
    return str(texto).encode('latin-1', 'replace').decode('latin-1')


class PadariaPDF(FPDF):
    def header(self):
        self.set_font('Helvetica', 'B', 14)
        self.cell(0, 8, 'O Pao Padaria Artesanal', align='C', new_x='LMARGIN', new_y='NEXT')
        self.set_font('Helvetica', '', 8)
        self.cell(0, 4, '', new_x='LMARGIN', new_y='NEXT')
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self.set_font('Helvetica', 'I', 8)
        self.cell(0, 10, f'Pagina {self.page_no()}', align='C')


def gerar_holerite(folha):
    """Gera PDF de holerite para uma folha de pagamento."""
    pdf = PadariaPDF()
    pdf.add_page()

    func = folha.funcionario

    # Titulo
    pdf.set_font('Helvetica', 'B', 12)
    pdf.cell(0, 8, 'RECIBO DE PAGAMENTO', align='C', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(4)

    # Dados do funcionario
    pdf.set_font('Helvetica', '', 10)
    pdf.cell(95, 6, f'Funcionario: {func.nome}', new_x='RIGHT')
    pdf.cell(95, 6, f'CPF: {func.cpf or "N/A"}', new_x='LMARGIN', new_y='NEXT')
    pdf.cell(95, 6, f'Funcao: {func.funcao or "N/A"}', new_x='RIGHT')
    pdf.cell(95, 6, f'Admissao: {func.data_admissao.strftime("%d/%m/%Y") if func.data_admissao else "N/A"}',
             new_x='LMARGIN', new_y='NEXT')
    pdf.cell(95, 6, f'Competencia: {folha.mes:02d}/{folha.ano}', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(4)

    # Proventos
    pdf.set_font('Helvetica', 'B', 10)
    pdf.set_fill_color(230, 230, 230)
    pdf.cell(130, 7, 'DESCRICAO', border=1, fill=True)
    pdf.cell(60, 7, 'VALOR (R$)', border=1, align='C', fill=True, new_x='LMARGIN', new_y='NEXT')

    pdf.set_font('Helvetica', '', 10)

    linhas = [
        ('Salario Base', folha.salario_base),
        ('Cargo de Confianca', folha.cargo_confianca),
        ('Horas Extras', folha.horas_extras),
        ('Premiacao', folha.premiacao),
        ('Vale Transporte', folha.vt),
        ('Vale Refeicao', folha.vr),
    ]

    total_proventos = 0
    for desc, valor in linhas:
        if not valor:
            continue
        total_proventos += valor
        pdf.cell(130, 6, f'  {desc}', border='LR')
        pdf.cell(60, 6, f'{valor:.2f}', border='LR', align='R', new_x='LMARGIN', new_y='NEXT')

    # Descontos
    descontos = folha.descontos or 0
    if descontos:
        pdf.cell(130, 6, '  (-) Descontos', border='LR')
        pdf.cell(60, 6, f'{descontos:.2f}', border='LR', align='R', new_x='LMARGIN', new_y='NEXT')

    # Total
    liquido = total_proventos - descontos
    pdf.set_font('Helvetica', 'B', 10)
    pdf.cell(130, 7, 'TOTAL LIQUIDO', border=1, fill=True)
    pdf.cell(60, 7, f'{liquido:.2f}', border=1, align='R', fill=True, new_x='LMARGIN', new_y='NEXT')

    pdf.ln(10)

    # Assinaturas
    pdf.set_font('Helvetica', '', 9)
    pdf.cell(95, 6, '____________________________________', align='C', new_x='RIGHT')
    pdf.cell(95, 6, '____________________________________', align='C', new_x='LMARGIN', new_y='NEXT')
    pdf.cell(95, 5, 'Empregador', align='C', new_x='RIGHT')
    pdf.cell(95, 5, 'Funcionario', align='C', new_x='LMARGIN', new_y='NEXT')

    return pdf.output()


# ── Impressao de pedidos de entrega (1 folha A4 por pedido por via) ──────
#
# Motivo de existir (11/06/2026): a impressao via HTML + window.print()
# quebrou no Safari de 3 jeitos diferentes (paginas duplicadas, paginas em
# branco, conteudo apagado ao imprimir). PDF gerado no servidor e
# CONGELADO: o navegador so exibe/imprime bytes prontos — sem re-layout,
# sem re-fetch, sem engine de paginacao do browser. O numero de paginas e
# garantido aqui: len(pedidos) * len(vias).
#
# Layout espelha o template entregas/imprimir.html (que segue como preview
# de tela): cabecalho com code + via, entrega/janela, destinatario,
# cartinha (so cliente), itens (valores so cliente), observacao,
# conferencia (so motorista) / total (so cliente).

_MARGEM = 14          # mm, igual ao @page do HTML
_LARGURA_UTIL = 210 - 2 * _MARGEM


def _campo_valor_item(it):
    """Valor de um item: forma REAL do VNDA usa subtotal/preco_unitario;
    valor_total/valor_unitario mantidos por compat com pedidos locais
    antigos (mesma cadeia do template HTML)."""
    vt = it.get('subtotal')
    if vt in (None, ''):
        vt = it.get('valor_total')
    if vt not in (None, ''):
        try:
            return float(vt)
        except (TypeError, ValueError):
            pass
    vu = it.get('preco_unitario')
    if vu in (None, ''):
        vu = it.get('valor_unitario') or 0
    q = it.get('quantidade') or 1
    try:
        return float(vu) * float(q)
    except (TypeError, ValueError):
        return 0.0


def _moeda(v):
    try:
        return ('R$ %.2f' % float(v)).replace('.', ',')
    except (TypeError, ValueError):
        return 'R$ 0,00'


def _total_pedido(p):
    """Total do pedido: `total` (campo real do VNDA) com fallback
    valor_total (compat pedidos locais antigos)."""
    tot = p.get('total')
    if tot in (None, ''):
        tot = p.get('valor_total') or 0
    try:
        return float(tot)
    except (TypeError, ValueError):
        return 0.0


def itens_sem_valor(p):
    """True quando o VNDA nao informou valor por item (todos zerados)
    mas o pedido tem total > 0 — acontece com produtos tipo kit/box
    ('Box Mimo'), onde o dinheiro so existe no total do pedido. Nesses
    casos imprimir 'R$ 0,00' por item e afirmacao falsa pro cliente —
    a coluna Valor e omitida e fica so o Total. (Rateio do total entre
    itens seria INVENTAR dado financeiro — nao fazemos.)

    Se apenas ALGUNS itens sao 0 (brinde legitimo junto de itens
    pagos), devolve False — o R$ 0,00 do brinde e informacao real."""
    if not isinstance(p, dict):
        return False
    itens = [it for it in (p.get('itens') or []) if isinstance(it, dict)]
    if not itens:
        return False
    if _total_pedido(p) <= 0:
        return False
    return all(_campo_valor_item(it) <= 0 for it in itens)


def _folha_pedido(pdf, p, via, data_fmt):
    """Desenha UMA folha (pagina A4) de um pedido numa via."""
    motorista = via == 'motorista'
    pdf.add_page()

    # Cabecalho: code + selo da via
    pdf.set_font('Helvetica', 'B', 20)
    pdf.cell(130, 10, _latin1(f'Pedido #{p.get("code") or "—"}'),
             new_x='RIGHT', new_y='TOP')
    pdf.set_font('Helvetica', 'B', 10)
    selo = 'VIA DO ENTREGADOR' if motorista else 'VIA DO CLIENTE'
    if motorista:
        pdf.set_fill_color(0, 0, 0)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(52, 8, selo, border=1, align='C', fill=True,
                 new_x='LMARGIN', new_y='NEXT')
        pdf.set_text_color(0, 0, 0)
    else:
        pdf.cell(52, 8, selo, border=1, align='C',
                 new_x='LMARGIN', new_y='NEXT')
    pdf.set_line_width(0.8)
    pdf.line(_MARGEM, pdf.get_y() + 2, 210 - _MARGEM, pdf.get_y() + 2)
    pdf.set_line_width(0.2)
    pdf.ln(6)

    # Entrega + janela
    pdf.set_font('Helvetica', 'B', 9)
    pdf.cell(40, 5, 'ENTREGA', new_x='RIGHT')
    pdf.cell(0, 5, 'JANELA', new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', 'B', 13)
    pdf.cell(40, 7, data_fmt, new_x='RIGHT')
    if p.get('expresso'):
        janela = 'EXPRESSO (1h)'
    else:
        janela = p.get('periodo') or '—'
    pdf.cell(0, 7, _latin1(janela), new_x='LMARGIN', new_y='NEXT')
    if not motorista and p.get('driver_nome'):
        pdf.set_font('Helvetica', '', 10)
        pdf.cell(0, 6, _latin1(f'Entregador: {p["driver_nome"]}'),
                 new_x='LMARGIN', new_y='NEXT')
    pdf.ln(4)

    # Destinatario
    pdf.set_font('Helvetica', 'B', 9)
    pdf.cell(0, 5, _latin1('DESTINATÁRIO'), new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', 'B', 15)
    pdf.multi_cell(0, 7, _latin1(p.get('destinatario')
                                 or p.get('comprador') or '—'),
                   new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 12)
    pdf.multi_cell(0, 6, _latin1(p.get('endereco') or '—'),
                   new_x='LMARGIN', new_y='NEXT')
    if p.get('telefone'):
        pdf.set_font('Helvetica', 'B', 12)
        pdf.cell(0, 7, _latin1(f'Tel: {p["telefone"]}'),
                 new_x='LMARGIN', new_y='NEXT')
    pdf.ln(3)

    # Cartinha (so via do cliente)
    if p.get('cartinha') and not motorista:
        pdf.set_font('Helvetica', 'B', 9)
        pdf.cell(0, 5, _latin1('CARTINHA (escrever à mão)'),
                 new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Helvetica', '', 12)
        pdf.multi_cell(_LARGURA_UTIL - 4, 6, _latin1(p['cartinha']),
                       border=1, new_x='LMARGIN', new_y='NEXT',
                       padding=2)
        pdf.ln(3)

    # Itens. `mostrar_valores`: na via do cliente, a coluna VALOR so
    # aparece quando o VNDA informou valor por item — produto tipo
    # kit/box vem com itens zerados e o dinheiro so no total (ver
    # itens_sem_valor); imprimir R$ 0,00 pro cliente e falso.
    itens = [it for it in (p.get('itens') or []) if isinstance(it, dict)]
    mostrar_valores = not motorista and not itens_sem_valor(p)
    total_unidades = 0
    for it in itens:
        try:
            total_unidades += int(it.get('quantidade') or 0)
        except (TypeError, ValueError):
            pass
    pdf.set_font('Helvetica', 'B', 9)
    pdf.cell(0, 5, _latin1(
        f'ITENS ({total_unidades} '
        f'{"item" if total_unidades == 1 else "itens"})'),
        new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', 'B', 9)
    pdf.set_fill_color(235, 235, 235)
    if not mostrar_valores:
        pdf.cell(24, 6, 'QTD', border=1, align='R', fill=True)
        pdf.cell(0, 6, '  ITEM', border=1, fill=True,
                 new_x='LMARGIN', new_y='NEXT')
    else:
        pdf.cell(24, 6, 'QTD', border=1, align='R', fill=True)
        pdf.cell(118, 6, '  ITEM', border=1, fill=True)
        pdf.cell(40, 6, 'VALOR  ', border=1, align='R', fill=True,
                 new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 10)
    for it in itens:
        try:
            qtd = int(it.get('quantidade') or 1)
        except (TypeError, ValueError):
            qtd = 1
        nome = _latin1(it.get('nome') or '—')
        if not mostrar_valores:
            pdf.cell(24, 6, f'{qtd}x ', border='B', align='R')
            pdf.cell(0, 6, f'  {nome}'[:90], border='B',
                     new_x='LMARGIN', new_y='NEXT')
        else:
            pdf.cell(24, 6, f'{qtd}x ', border='B', align='R')
            pdf.cell(118, 6, f'  {nome}'[:80], border='B')
            pdf.cell(40, 6, _moeda(_campo_valor_item(it)) + '  ',
                     border='B', align='R', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(3)

    # Observacao
    if p.get('observacao'):
        pdf.set_font('Helvetica', 'B', 9)
        pdf.cell(0, 5, _latin1('OBSERVAÇÃO'), new_x='LMARGIN',
                 new_y='NEXT')
        pdf.set_font('Helvetica', '', 10)
        pdf.multi_cell(0, 5, _latin1(p['observacao']),
                       new_x='LMARGIN', new_y='NEXT')
        pdf.ln(2)

    if motorista:
        # Conferencia da entrega (linhas pra preencher a mao)
        pdf.ln(6)
        pdf.set_font('Helvetica', 'B', 9)
        pdf.cell(0, 5, _latin1('CONFERÊNCIA DA ENTREGA'),
                 new_x='LMARGIN', new_y='NEXT')
        pdf.ln(8)
        pdf.set_font('Helvetica', '', 8)
        pdf.cell(110, 5, 'recebido por', new_x='RIGHT')
        pdf.cell(0, 5, _latin1('horário'), new_x='LMARGIN', new_y='NEXT')
        y = pdf.get_y()
        pdf.line(_MARGEM, y, _MARGEM + 105, y)
        pdf.line(_MARGEM + 112, y, 210 - _MARGEM, y)
        pdf.ln(12)
        pdf.cell(0, 5, 'assinatura', new_x='LMARGIN', new_y='NEXT')
        y = pdf.get_y()
        pdf.line(_MARGEM, y, 210 - _MARGEM, y)
    else:
        # Total (so via do cliente)
        tot = _total_pedido(p)
        pdf.ln(2)
        pdf.set_line_width(0.6)
        pdf.line(_MARGEM, pdf.get_y(), 210 - _MARGEM, pdf.get_y())
        pdf.set_line_width(0.2)
        pdf.ln(2)
        pdf.set_font('Helvetica', 'B', 14)
        pdf.cell(91, 8, 'Total')
        pdf.cell(91, 8, _moeda(tot), align='R',
                 new_x='LMARGIN', new_y='NEXT')

    # Rodape da folha
    pdf.set_y(-20)
    pdf.set_font('Helvetica', 'I', 8)
    pdf.set_text_color(100, 100, 100)
    via_lbl = 'entregador' if motorista else 'cliente'
    pdf.cell(95, 5, _latin1('O Pão Padaria Artesanal'), new_x='RIGHT')
    pdf.cell(0, 5, _latin1(f'Pedido {p.get("code") or "—"} · {via_lbl}'),
             align='R', new_x='LMARGIN', new_y='NEXT')
    pdf.set_text_color(0, 0, 0)


def montar_pedidos_pdf(pedidos, vias, data):
    """Monta o objeto FPDF com 1 pagina por pedido por via. Separado do
    gerar_ pra teste poder afirmar a contagem de paginas (garantia
    central: paginas == len(pedidos) * len(vias))."""
    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.set_margins(_MARGEM, _MARGEM, _MARGEM)
    pdf.set_auto_page_break(auto=False)
    data_fmt = data.strftime('%d/%m/%Y')
    for p in pedidos:
        if not isinstance(p, dict):
            continue
        for via in vias:
            _folha_pedido(pdf, p, via, data_fmt)
    return pdf


def gerar_pedidos_pdf(pedidos, vias, data):
    """PDF de impressao de pedidos: bytes prontos pro browser."""
    return bytes(montar_pedidos_pdf(pedidos, vias, data).output())


# ── Orcamento B2B ─────────────────────────────────────────────────────
def gerar_orcamento_pdf(orc):
    """PDF de orcamento B2B (1 pagina, A4). Cabecalho com identificacao
    da padaria, dados do cliente, tabela de itens, totais, observacao
    e validade. Pronto pra mandar via WhatsApp/e-mail.
    """
    from decimal import Decimal

    pdf = PadariaPDF(orientation='P', unit='mm', format='A4')
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # Cabecalho (PadariaPDF.header ja imprimiu "O Pao Padaria Artesanal").
    pdf.set_font('Helvetica', 'B', 13)
    pdf.cell(0, 8, _latin1(f'ORCAMENTO {orc.codigo}'),
             new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 9)
    pdf.cell(0, 5, _latin1(f'Emitido em {orc.data.strftime("%d/%m/%Y")} · '
                           f'Valido ate {orc.valido_ate.strftime("%d/%m/%Y")}'),
             new_x='LMARGIN', new_y='NEXT')
    entrega_txt = (orc.data_entrega.strftime('%d/%m/%Y')
                   if orc.data_entrega else 'a combinar')
    pdf.cell(0, 5, _latin1(f'Entrega prevista: {entrega_txt}'),
             new_x='LMARGIN', new_y='NEXT')
    pdf.ln(2)

    # Cliente
    pdf.set_font('Helvetica', 'B', 10)
    pdf.cell(0, 6, 'CLIENTE', new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 9)
    nome = orc.cliente.nome if orc.cliente else (orc.cliente_nome or '-')
    pdf.cell(0, 5, _latin1(nome), new_x='LMARGIN', new_y='NEXT')
    extras = []
    cnpj = (orc.cliente.cnpj_cpf if orc.cliente else None) or orc.cliente_documento
    if cnpj:
        extras.append(f'CNPJ/CPF: {cnpj}')
    tel = (orc.cliente.telefone if orc.cliente else None) or orc.cliente_telefone
    if tel:
        extras.append(f'Tel: {tel}')
    email = (orc.cliente.email if orc.cliente else None) or orc.cliente_email
    if email:
        extras.append(email)
    if extras:
        pdf.cell(0, 5, _latin1(' · '.join(extras)),
                 new_x='LMARGIN', new_y='NEXT')
    end = (orc.cliente.endereco if orc.cliente else None) or orc.cliente_endereco
    if end:
        pdf.cell(0, 5, _latin1(end), new_x='LMARGIN', new_y='NEXT')
    pdf.ln(3)

    # Tabela de itens
    pdf.set_font('Helvetica', 'B', 9)
    pdf.set_fill_color(230, 230, 230)
    pdf.cell(95, 7, 'ITEM', border=1, fill=True)
    pdf.cell(20, 7, 'QTD', border=1, align='R', fill=True)
    pdf.cell(15, 7, 'UN', border=1, align='C', fill=True)
    pdf.cell(25, 7, 'PRECO', border=1, align='R', fill=True)
    pdf.cell(25, 7, 'SUBTOTAL', border=1, align='R', fill=True,
             new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 9)
    for it in orc.itens:
        qtd = float(Decimal(str(it.quantidade or 0)))
        qtd_str = (f'{qtd:.0f}' if qtd == int(qtd) else f'{qtd:.3f}'.rstrip('0').rstrip('.'))
        preco = float(Decimal(str(it.preco_unitario or 0)))
        sub = float(Decimal(str(it.subtotal or 0)))
        nome = (it.nome or '')
        if it.observacao:
            nome = f'{nome}  ({it.observacao})'
        # Quebra de linha se o item for muito longo: usa multi_cell pra
        # primeira coluna, depois reposiciona y pra alinhar as outras.
        y_ini = pdf.get_y()
        x_ini = pdf.get_x()
        pdf.multi_cell(95, 6, _latin1(nome), border=1)
        y_fim = pdf.get_y()
        altura = max(6, y_fim - y_ini)
        pdf.set_xy(x_ini + 95, y_ini)
        pdf.cell(20, altura, qtd_str, border=1, align='R')
        pdf.cell(15, altura, _latin1(it.unidade or '-'), border=1, align='C')
        pdf.cell(25, altura, f'R$ {preco:.2f}'.replace('.', ','),
                 border=1, align='R')
        pdf.cell(25, altura, f'R$ {sub:.2f}'.replace('.', ','),
                 border=1, align='R', new_x='LMARGIN', new_y='NEXT')

    pdf.ln(2)

    # Totais
    sub = float(Decimal(str(orc.subtotal or 0)))
    desc = float(Decimal(str(orc.desconto_valor or 0)))
    tot = float(Decimal(str(orc.valor_total or 0)))
    pdf.set_font('Helvetica', '', 10)
    pdf.cell(155, 6, 'Subtotal:', align='R')
    pdf.cell(25, 6, f'R$ {sub:.2f}'.replace('.', ','),
             align='R', new_x='LMARGIN', new_y='NEXT')
    if desc > 0:
        pdf.cell(155, 6, 'Desconto:', align='R')
        pdf.cell(25, 6, f'- R$ {desc:.2f}'.replace('.', ','),
                 align='R', new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', 'B', 11)
    pdf.cell(155, 8, 'TOTAL:', align='R')
    pdf.cell(25, 8, f'R$ {tot:.2f}'.replace('.', ','),
             align='R', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(4)

    if orc.observacao:
        pdf.set_font('Helvetica', 'B', 9)
        pdf.cell(0, 5, 'OBSERVACOES', new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Helvetica', '', 9)
        pdf.multi_cell(0, 5, _latin1(orc.observacao))
        pdf.ln(2)

    # Bloco PIX — pagamento. Constantes em app.constants pra ficar 1
    # fonte de verdade do CNPJ/razao social.
    from app.constants import (
        PADARIA_CHAVE_PIX,
        PADARIA_PIX_TIPO,
        PADARIA_RAZAO_SOCIAL,
    )
    pdf.set_font('Helvetica', 'B', 9)
    # Hifen ASCII no titulo: em-dash (—) nao existe na Helvetica latin-1
    # e quebra o render. `_latin1` substituiria por '?' — pior.
    pdf.cell(0, 5, 'PAGAMENTO - PIX', new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 9)
    pdf.cell(0, 5, _latin1(
        f'Chave PIX ({PADARIA_PIX_TIPO}): {PADARIA_CHAVE_PIX}'),
        new_x='LMARGIN', new_y='NEXT')
    pdf.cell(0, 5, _latin1(f'Favorecido: {PADARIA_RAZAO_SOCIAL}'),
             new_x='LMARGIN', new_y='NEXT')
    pdf.ln(2)

    # Rodape com aceite
    pdf.set_font('Helvetica', 'I', 8)
    pdf.cell(0, 5,
             _latin1('Proposta valida ate a data indicada. Apos confirmar o '
                     'PIX, envie o comprovante para fechar o pedido.'),
             new_x='LMARGIN', new_y='NEXT')
    return bytes(pdf.output())

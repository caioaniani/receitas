from fpdf import FPDF


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

"""Pacote de cobrança para compartilhamento manual, sem emissão ou envio.

PDF único para WhatsApp; ZIP mantém os arquivos originais, inclusive assinaturas.
Não grava EnvioCobranca: download não comprova que alguém enviou ao cliente.
"""
from decimal import Decimal
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from fpdf.fonts import FontFace
from pypdf import PdfReader, PdfWriter

from app.services.pdf import PadariaPDF, _latin1

_LIMITE_BYTES = 20 * 1024 * 1024
_LIMITE_PAGINAS = 150
_ZERO = Decimal('0.00')


def _dinheiro(valor):
    return ('R$ ' + f'{Decimal(valor or 0):,.2f}'
            .replace(',', '_').replace('.', ',').replace('_', '.'))


def _texto(pdf, texto, tamanho=10, negrito=False):
    pdf.set_font('Helvetica', 'B' if negrito else '', tamanho)
    pdf.multi_cell(0, 5.5, _latin1(texto), new_x='LMARGIN', new_y='NEXT')


def _vendas(r):
    vendas = (sorted(r.documento.vendas, key=lambda v: (v.data_venda, v.id))
              if r.tipo == 'fatura' else [r.documento])
    if not vendas or any(v.status == 'cancelada' or v.sem_cobranca for v in vendas):
        raise ValueError('Confira os pedidos vinculados na origem antes de baixar a cobrança.')
    if r.tipo == 'fatura' and sum((v.valor_total for v in vendas), _ZERO) != r.valor:
        raise ValueError('O total dos pedidos difere da fatura. Confira o fechamento na origem.')
    if r.tipo == 'fatura' and any(v.cliente_id != r.documento.cliente_id for v in vendas):
        raise ValueError('Há um pedido de outro cliente nesta fatura. Confira o fechamento na origem.')
    for v in vendas:
        if not v.itens:
            raise ValueError(f'O pedido da venda #{v.id} não tem itens cadastrados. Confira a origem.')
        total_itens = sum((it.valor_total for it in v.itens), _ZERO)
        if total_itens + (v.frete_valor or _ZERO) != v.valor_total:
            raise ValueError(f'Os itens e o frete da venda #{v.id} diferem do total. Confira a origem.')
    return vendas


def gerar_pedidos_cobranca_pdf(r):
    """Detalhamento próprio de venda/fatura B2B; não reutiliza orçamento/PIX.

    Mostra o valor da parcela separado do total do pedido. Nas faturas, inclui
    todas as vendas vinculadas e somente elas, sem cobrar parcelas em duplicidade.
    """
    vendas = _vendas(r)
    pdf = PadariaPDF()
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(True, margin=20)
    pdf.set_title(_latin1(f'Pedidos - {r.referencia}'))
    pdf.set_author('O Pao Padaria Artesanal')
    for indice, venda in enumerate(vendas):
        pdf.add_page()
        pdf.set_text_color(39, 93, 69)
        _texto(pdf, 'DETALHAMENTO DOS PEDIDOS' if r.tipo == 'fatura' else 'DETALHAMENTO DO PEDIDO', 14, True)
        pdf.ln(3)
        pdf.set_text_color(25, 35, 30)
        _texto(pdf, r.cliente, 12, True)
        cliente = r.documento.cliente
        if cliente and cliente.cnpj_cpf:
            _texto(pdf, f'CNPJ/CPF: {cliente.cnpj_cpf}', 9)
        _texto(pdf, f'Referência: {r.referencia}', 10)
        if r.tipo == 'fatura':
            _texto(pdf, f'Período: {r.documento.periodo_display} | Pedido {indice + 1} de {len(vendas)}', 9)
        _texto(pdf, f'Valor desta cobrança: {_dinheiro(r.saldo)} | Vencimento: {r.vencimento:%d/%m/%Y}', 10, True)
        if r.tipo == 'parcela':
            _texto(pdf, 'O boleto corresponde apenas à parcela indicada acima, não necessariamente ao total do pedido.', 9)
        pdf.ln(5)
        _texto(pdf, f'Pedido / venda #{venda.id} - {venda.data_venda:%d/%m/%Y}', 11, True)
        if venda.data_entrega:
            _texto(pdf, f'Data de entrega cadastrada: {venda.data_entrega:%d/%m/%Y}', 9)
        pdf.ln(3)
        pdf.set_font('Helvetica', '', 9)
        with pdf.table(col_widths=(85, 14, 27, 20, 34), line_height=5.5,
                       text_align=('LEFT', 'RIGHT', 'RIGHT', 'RIGHT', 'RIGHT'),
                       padding=2, borders_layout='HORIZONTAL_LINES',
                       headings_style=FontFace(emphasis='BOLD', fill_color=(232, 242, 236))) as table:
            table.row(('Item', 'Qtd.', 'Unitário', 'Desc. %', 'Total'))
            for item in sorted(venda.itens, key=lambda it: it.id):
                desconto = format(Decimal(str(item.desconto_percentual or 0)).normalize(), 'f')
                # Não incluir notas internas da venda no documento do cliente.
                table.row((_latin1(item.nome_item_com_estado), str(item.quantidade),
                           _dinheiro(item.preco_unitario), desconto.replace('.', ','),
                           _dinheiro(item.valor_total)))
        pdf.ln(4)
        # Reserva espaço para os totais, inclusive com itens de várias linhas.
        # PadariaPDF.header usa get_y(), incompatível com unbreakable do fpdf2.
        if pdf.will_page_break(38):
            pdf.add_page()
        _texto(pdf, f'Produtos (após descontos): {_dinheiro(sum((it.valor_total for it in venda.itens), _ZERO))}', 10)
        _texto(pdf, f'Frete: {_dinheiro(venda.frete_valor)}', 10)
        _texto(pdf, f'Total do pedido: {_dinheiro(venda.valor_total)}', 11, True)
        pdf.ln(3)
        _texto(pdf, 'Detalhamento para conferência. Não substitui a nota fiscal e não é comprovante de pagamento.', 9)
    return bytes(pdf.output())


def _ler_pdf(conteudo, titulo):
    try:
        if not conteudo or not bytes(conteudo).startswith(b'%PDF') or len(conteudo) > _LIMITE_BYTES:
            raise ValueError
        reader = PdfReader(BytesIO(bytes(conteudo)))
        if reader.is_encrypted or not 0 < len(reader.pages) <= _LIMITE_PAGINAS:
            raise ValueError
        return reader
    except Exception as exc:
        raise ValueError(f'{titulo}: PDF indisponível, protegido ou inválido. Nenhum pacote foi baixado.') from exc


def _assinado(reader):
    return bool(reader.root_object.get('/Perms') or any(
        campo.get('/FT') == '/Sig' for campo in (reader.get_fields() or {}).values()))


def baixar_pacote(r, formato='pdf', banco_confirmado=False):
    """Leituras + montagem em memória. Nunca emite NF/boleto nem dispara mensagem."""
    from app.services import tiny_nf
    from app.services.sicredi_boleto import gerar_boleto_pdf

    if formato not in ('pdf', 'zip'):
        raise ValueError('Escolha PDF único ou PDFs separados (ZIP).')
    if r.bloqueio:
        raise ValueError(r.bloqueio)
    if r.cobranca.status == 'remessa' and not banco_confirmado:
        raise ValueError('Confirme o registro do boleto no Sicredi antes de baixar para cobrar o cliente.')

    # Valida os pedidos antes de consultar o provedor. Não entrega pacote parcial.
    pedidos = gerar_pedidos_cobranca_pdf(r)
    nf, _motivo = tiny_nf.baixar_danfe_pdf_com_motivo(r.documento.tiny_nota_fiscal_id)
    boleto = bytes(gerar_boleto_pdf(r.cobranca))
    arquivos = [('01-nota-fiscal.pdf', nf, 'Nota fiscal (DANFE)'),
                ('02-boleto.pdf', boleto, 'Boleto'),
                ('03-pedidos.pdf', pedidos, 'Detalhamento dos pedidos')]
    leitores = [(nome, conteudo, titulo, _ler_pdf(conteudo, titulo)) for nome, conteudo, titulo in arquivos]
    referencia = (f'fatura-{r.id}' if r.tipo == 'fatura'
                  else f'venda-{r.documento.id}-parcela-{r.cobranca.parcela.numero}')
    nome = f'cobranca-{referencia}.{formato}'
    saida = BytesIO()
    if formato == 'zip':
        with ZipFile(saida, 'w', ZIP_DEFLATED) as pacote:
            for arquivo, conteudo, _, _ in leitores:
                pacote.writestr(arquivo, conteudo)
        return saida.getvalue(), nome, 'application/zip'

    with PdfWriter() as pdf:
        for _, _, titulo, reader in leitores:
            if _assinado(reader):
                raise ValueError('Há um PDF assinado digitalmente. Use “PDFs separados (ZIP)” para preservar a assinatura original.')
            pdf.append(reader, outline_item=titulo, import_outline=False)
        pdf.add_metadata({'/Title': f'Cobrança - {r.referencia}', '/Author': 'O Pao Padaria Artesanal'})
        pdf.write(saida)
    return saida.getvalue(), nome, 'application/pdf'

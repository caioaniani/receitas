"""Download completo: documentos corretos, privado e estritamente sem disparos."""
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch
from zipfile import ZipFile

import pytest
from fpdf import FPDF
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject

from app.extensions import db
from app.models import EnvioCobranca, Usuario, VendaB2BItem, VendaB2BParcela
from app.services.central_cobrancas import carregar
from app.services.cobrancas_download import baixar_pacote, gerar_pedidos_cobranca_pdf
from tests.conftest import _make_receita
from tests.test_b2b_email_docs import _cenario, _preparar_nf
from tests.test_central_cobrancas import _client, _mensal


def _pdf(*paginas):
    pdf = FPDF()
    for texto in paginas:
        pdf.add_page()
        pdf.set_font('Helvetica', size=12)
        pdf.cell(0, 10, texto)
    return bytes(pdf.output())


def _texto(conteudo):
    return '\n'.join(p.extract_text() for p in PdfReader(BytesIO(conteudo)).pages)


def _itens(venda, quantidade=20, preco=25, desconto=0, nome='Brioche'):
    receita = _make_receita(nome)
    db.session.add(receita)
    db.session.flush()
    item = VendaB2BItem(venda_id=venda.id, receita_id=receita.id, quantidade=quantidade,
                       preco_unitario=Decimal(preco), desconto_percentual=desconto)
    db.session.add(item)
    db.session.commit()
    return item


@pytest.fixture
def cenario(app):
    cli, venda, parcela, cob = _cenario()
    _preparar_nf(venda)
    _itens(venda)
    return cli, venda, parcela, cob


def _url(parcela, formato='pdf'):
    return f'/cobrancas/parcela/{parcela.id}/baixar?formato={formato}'


@pytest.mark.parametrize('formato', ['pdf', 'zip'])
def test_download_completo_privado_sem_alterar_financeiro_ou_historico(app, admin_user, cenario, formato):
    _, venda, parcela, cob = cenario
    antes = (venda.valor_total, venda.nf_emitida_em, venda.estoque_baixado_em,
             parcela.valor_pago, cob.nosso_numero, cob.status)
    danfe = _pdf('DANFE demonstracao pagina 1', 'DANFE demonstracao pagina 2')
    boleto = _pdf('BOLETO demonstracao')
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo', return_value=(danfe, None)) as nf, \
            patch('app.services.sicredi_boleto.gerar_boleto_pdf', return_value=boleto), \
            patch('app.services.email.enviar') as enviar, \
                patch('app.services.tiny_nf_b2b.emitir_nf') as emitir:
        response = _client(app, admin_user).get(_url(parcela, formato))
    assert response.status_code == 200
    assert response.mimetype == ('application/pdf' if formato == 'pdf' else 'application/zip')
    assert response.headers['Content-Disposition'] == f'attachment; filename="Restaurante Bom Prato - Entrega sem data - Pedido {venda.id} - NF 11629.{formato}"'
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    nf.assert_called_once_with(venda.tiny_nota_fiscal_id)
    enviar.assert_not_called()
    emitir.assert_not_called()
    assert EnvioCobranca.query.count() == 0
    assert (venda.valor_total, venda.nf_emitida_em, venda.estoque_baixado_em,
            parcela.valor_pago, cob.nosso_numero, cob.status) == antes
    if formato == 'pdf':
        reader = PdfReader(BytesIO(response.data))
        assert len(reader.pages) == 4
        assert 'DANFE demonstracao pagina 1' in reader.pages[0].extract_text()
        assert 'DANFE demonstracao pagina 2' in reader.pages[1].extract_text()
        assert 'BOLETO demonstracao' in reader.pages[2].extract_text()
        assert 'Brioche' in reader.pages[3].extract_text()
        assert len(reader.outline) == 3
    else:
        with ZipFile(BytesIO(response.data)) as pacote:
            assert pacote.namelist() == ['01-nota-fiscal.pdf', '02-boleto.pdf', '03-pedidos.pdf']
            assert pacote.read('01-nota-fiscal.pdf') == danfe
            assert pacote.read('02-boleto.pdf') == boleto
            assert 'Brioche' in _texto(pacote.read('03-pedidos.pdf'))


def test_boleto_real_conserva_linha_digitavel_no_pdf_unico(app, cenario):
    from app.services.sicredi_boleto import codigo_barras_da_cobranca, linha_digitavel
    _, _, parcela, cob = cenario
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo', return_value=(_pdf('DANFE'), None)):
        pdf, _, _ = baixar_pacote(carregar('parcela', parcela.id))
    texto = _texto(pdf)
    assert linha_digitavel(codigo_barras_da_cobranca(cob)) in texto
    assert '25/200004-1' in texto


def test_fatura_inclui_todos_pedidos_mas_nenhuma_venda_externa(app):
    fatura, parcela, _ = _mensal()
    fatura.cliente.nome = 'Cliente da fatura'
    _itens(parcela.venda, nome='Pao da primeira entrega')
    cli2, segunda, _, _ = _cenario(nosso_numero='252000042')
    cli2.nome = 'Cliente secundario'
    segunda.fatura_id = fatura.id
    # O mesmo cliente da fatura; outra venda fora do fechamento não pode vazar.
    segunda.cliente_id = fatura.cliente_id
    fatura.valor_total += segunda.valor_total
    db.session.commit()
    _itens(segunda, nome='Croissant da segunda entrega')
    _, fora, _, _ = _cenario(nosso_numero='252000043')
    _itens(fora, nome='Nao incluir este pedido')
    pdf = gerar_pedidos_cobranca_pdf(carregar('fatura', fatura.id))
    reader = PdfReader(BytesIO(pdf))
    assert len(reader.pages) == 2
    texto = _texto(pdf)
    assert 'Pao da primeira entrega' in texto and 'Croissant da segunda entrega' in texto
    assert 'Nao incluir este pedido' not in texto
    assert 'R$ 1.000,00' in texto and 'Pedido 2 de 2' in texto


def test_parcela_nao_confunde_valor_a_cobrar_com_total_pedido(app, cenario):
    _, venda, parcela, cob = cenario
    parcela.valor = cob.valor = Decimal('250')
    db.session.add(VendaB2BParcela(venda_id=venda.id, numero=2, vencimento=parcela.vencimento, valor=Decimal('250')))
    venda.observacao = 'NOTA INTERNA: nunca compartilhar'
    db.session.commit()
    texto = _texto(gerar_pedidos_cobranca_pdf(carregar('parcela', parcela.id)))
    assert 'Valor desta cobrança: R$ 250,00' in texto
    assert 'Total do pedido: R$ 500,00' in texto
    assert 'apenas à parcela' in texto
    assert 'NOTA INTERNA' not in texto


def test_pedido_frete_desconto_centavos_e_nome_longo_sem_truncar(app, cenario):
    _, venda, parcela, cob = cenario
    item = venda.itens[0]
    item.preco_unitario, item.quantidade, item.desconto_percentual = Decimal('19.99'), 27, 10
    item.receita.nome = 'Pão artesanal com fermentação natural, sementes e cobertura especial - embalagem para presente'
    venda.frete_valor = Decimal('18.97')
    venda.valor_total = parcela.valor = cob.valor = item.valor_total + venda.frete_valor
    db.session.commit()
    texto = _texto(gerar_pedidos_cobranca_pdf(carregar('parcela', parcela.id)))
    assert 'embalagem para presente' in texto
    assert '19,99' in texto and '485,76' in texto and '18,97' in texto and '504,73' in texto
    assert '10' in texto.split()


@pytest.mark.parametrize('nome', ['Brioche', 'Brioche artesanal com fermentação natural, sementes e cobertura especial - embalagem para presente'])
def test_muitos_itens_paginam_sem_perder_itens_ou_totais(app, cenario, nome):
    _, venda, parcela, cob = cenario
    receita = venda.itens[0].receita
    receita.nome = nome
    for i in range(100):
        db.session.add(VendaB2BItem(venda_id=venda.id, receita_id=receita.id, quantidade=i + 1, preco_unitario=Decimal('1.00')))
    db.session.commit()
    venda.valor_total = parcela.valor = cob.valor = sum(it.valor_total for it in venda.itens)
    db.session.commit()
    pdf = gerar_pedidos_cobranca_pdf(carregar('parcela', parcela.id))
    texto = _texto(pdf)
    assert len(PdfReader(BytesIO(pdf)).pages) > 3
    assert texto.count('Brioche') == 101
    assert 'Total do pedido: R$ 5.550,00' in texto
    assert texto.count('Unitário') > 1


@pytest.mark.parametrize('falha', ['nf', 'boleto', 'excecao', 'vazio', 'senha'])
@pytest.mark.parametrize('formato', ['pdf', 'zip'])
def test_documento_invalido_nao_baixa_pacote_parcial(app, admin_user, cenario, falha, formato):
    _, _, parcela, _ = cenario
    danfe = _pdf('DANFE')
    if falha == 'nf':
        danfe = b'%PDF-corrompido'
    elif falha in ('vazio', 'senha'):
        writer, saida = PdfWriter(), BytesIO()
        if falha == 'senha':
            writer.add_blank_page(width=595, height=842)
            writer.encrypt('protegido')
        writer.write(saida)
        danfe = saida.getvalue()
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo',
               return_value=(danfe, None), side_effect=TimeoutError if falha == 'excecao' else None), \
            patch('app.services.sicredi_boleto.gerar_boleto_pdf',
                  return_value=b'erro' if falha == 'boleto' else _pdf('boleto')), \
            patch('app.services.email.enviar') as enviar:
        response = _client(app, admin_user).get(_url(parcela, formato), follow_redirects=True)
    assert response.status_code == 200 and response.mimetype == 'text/html'
    assert 'Content-Disposition' not in response.headers
    assert 'Nenhum pacote foi baixado' in response.text or 'Não foi possível preparar os três documentos' in response.text
    enviar.assert_not_called()
    assert EnvioCobranca.query.count() == 0


@pytest.mark.parametrize('falha', ['sem_cobranca', 'cancelada', 'paga', 'nf', 'boleto', 'rejeitada', 'parcial', 'sem_itens', 'total'])
def test_bloqueios_impedem_download_antes_de_consultar_tiny(app, admin_user, cenario, falha):
    _, venda, parcela, cob = cenario
    if falha == 'sem_cobranca':
        venda.dispensa_cobranca = {'motivo': 'Divulgacao'}
    elif falha == 'cancelada':
        venda.status = 'cancelada'
    elif falha == 'paga':
        parcela.valor_pago = parcela.valor
    elif falha == 'nf':
        venda.nf_emitida_em = None
    elif falha == 'boleto':
        cob.nosso_numero = None
    elif falha == 'rejeitada':
        cob.status = 'rejeitada'
    elif falha == 'parcial':
        parcela.valor_pago = Decimal('10')
    elif falha == 'sem_itens':
        db.session.delete(venda.itens[0])
    elif falha == 'total':
        venda.valor_total += Decimal('1')
    db.session.commit()
    db.session.expire_all()
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo') as nf:
        response = _client(app, admin_user).get(_url(parcela), follow_redirects=True)
    assert response.mimetype == 'text/html'
    nf.assert_not_called()


def test_remessa_exige_confirmacao_explicita_sem_mudar_status(app, admin_user, cenario):
    _, _, parcela, cob = cenario
    cob.status = 'remessa'
    db.session.commit()
    client = _client(app, admin_user)
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo', return_value=(_pdf('DANFE'), None)) as nf:
        response = client.get(_url(parcela), follow_redirects=True)
        assert 'Confirme o registro do boleto no Sicredi' in response.text
        nf.assert_not_called()
        response = client.get(_url(parcela) + '&banco_confirmado=1')
        assert response.mimetype == 'application/pdf'
    assert cob.status == 'remessa'


@pytest.mark.parametrize('papel', [None, 'loja', 'producao', 'treinamento', 'observador'])
def test_download_restrito_a_administrador(app, cenario, papel):
    _, _, parcela, _ = cenario
    client = app.test_client()
    if papel:
        user = Usuario(nome='Sem permissão', login='restrito', papel=papel)
        user.set_senha('senha-ficticia-teste')
        db.session.add(user)
        db.session.commit()
        client = _client(app, user)
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo') as nf:
        response = client.get(_url(parcela))
    assert response.status_code == (302 if papel in (None, 'observador') else 403)
    if papel == 'observador':
        assert response.location.endswith('/pedidos/observador')
    nf.assert_not_called()


@pytest.mark.parametrize('origem', ['parcela', 'boleto'])
def test_link_absorvido_volta_para_fatura_sem_baixar_individual(app, admin_user, origem):
    fatura, parcela, cob = _mensal()
    ref = parcela.id if origem == 'parcela' else cob.id
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo') as nf:
        response = _client(app, admin_user).get(f'/cobrancas/{origem}/{ref}/baixar')
    assert response.location.endswith(f'/cobrancas/fatura/{fatura.id}/documentos')
    nf.assert_not_called()


def test_pdf_assinado_so_em_zip_sem_modificar_original(app, cenario):
    _, _, parcela, _ = cenario
    writer, saida = PdfWriter(), BytesIO()
    writer.add_blank_page(width=595, height=842)
    campo = DictionaryObject({NameObject('/FT'): NameObject('/Sig'), NameObject('/T'): TextStringObject('Assinatura')})
    writer.root_object[NameObject('/AcroForm')] = DictionaryObject({NameObject('/Fields'): ArrayObject([writer._add_object(campo)])})
    writer.write(saida)
    danfe = saida.getvalue()
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo', return_value=(danfe, None)):
        with pytest.raises(ValueError, match='assinado digitalmente'):
            baixar_pacote(carregar('parcela', parcela.id))
        pacote, _, _ = baixar_pacote(carregar('parcela', parcela.id), formato='zip')
    with ZipFile(BytesIO(pacote)) as zip_:
        assert zip_.read('01-nota-fiscal.pdf') == danfe


def test_tela_exibe_download_sem_exigir_email_e_sem_consultar_documentos(app, admin_user, cenario):
    cli, _, parcela, _ = cenario
    cli.email = None
    db.session.commit()
    with patch('app.services.tiny_nf.baixar_danfe_pdf_com_motivo') as nf:
        response = _client(app, admin_user).get(f'/cobrancas/parcela/{parcela.id}/documentos')
    assert 'Baixar cobrança completa (PDF)' in response.text
    assert 'PDFs separados (ZIP)' in response.text
    assert 'O download não envia mensagens' in response.text
    assert f'action="/cobrancas/parcela/{parcela.id}/baixar"' in response.text
    assert response.text.count('data-no-loading="1"') == 2
    nf.assert_not_called()

"""Recupera a NF corrigida no Tiny após uma recriação perder o vínculo local."""
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import Cobranca
from app.services import cobrancas_nf, tiny_nf
from tests.test_b2b_emitir_nf import _cliente_completo, _venda
from tests.test_b2b_nf_sincronizacao import _pendente


def _sem_vinculo(tipo='venda', *, cliente=None, paga=True):
    cliente = cliente or _cliente_completo()
    cliente.nome = 'Lavô Street Itaim Bibi'
    cliente.cnpj_cpf = '40.099.645/0001-48'
    doc, tentativa = _pendente(tipo, cliente=cliente, nota_id=None, paga=paga)
    doc.nf_numero = '012693'
    doc.nf_status = None
    doc.valor_total = Decimal('606.00')
    venda = doc if tipo == 'venda' else _venda(cliente)
    venda.valor_total = Decimal('606.00')
    venda.itens[0].quantidade = 1
    venda.itens[0].preco_unitario = Decimal('606.00')
    tiny_nf.definir_sku('produto', venda.itens[0].produto_id, 'SKU-NF-55', canal='b2b')
    if tipo == 'fatura':
        doc.vendas.append(venda)
    else:
        for parcela in doc.parcelas:
            parcela.valor = parcela.valor_pago = doc.valor_total
    tentativa.erro = 'Falha ao criar a NF no Tiny: Registro em duplicidade - Nota fiscal já cadastrada'
    db.session.flush()
    tentativa.assinatura = cobrancas_nf.assinatura_documento(doc)
    db.session.commit()
    return doc, tentativa


def _nota(**alteracoes):
    return {
        'id': '911314754', 'numero': '12693', 'serie': '1',
        'tipo_nota': 'N', 'finalidade': '1', 'situacao': '6', 'valor_nota': '606.00',
        'cliente': {'cpf_cnpj': '40099645000148', 'ie': '154962497111'},
        'itens': [_item()],
        **alteracoes,
    }


def _item(**alteracoes):
    return {'item': {'codigo': 'SKU-NF-55', 'quantidade': '1',
                     'valor_unitario': '606.00', **alteracoes}}


def _estado(doc, tentativa):
    db.session.refresh(doc)
    db.session.refresh(tentativa)
    return (doc.tiny_nota_fiscal_id, doc.nf_numero, doc.nf_status,
            doc.nf_emitida_em, tentativa.estado, tentativa.erro,
            tentativa.assinatura, tentativa.iniciada_em)


@pytest.fixture(autouse=True)
def sem_emissao_ou_cobranca():
    """A recuperação só pode consultar o provedor e atualizar o vínculo local."""
    with patch('app.services.tiny.incluir_nota_fiscal') as incluir, \
            patch('app.services.tiny.emitir_nota_fiscal') as emitir, \
            patch('app.services.sicredi_cnab.gerar_remessa') as remessa, \
            patch('app.services.cobrancas_envio.enviar_automatico') as enviar:
        yield
        for acao in (incluir, emitir, remessa, enviar):
            acao.assert_not_called()


@pytest.mark.parametrize('tipo,situacao', [('venda', '6'), ('fatura', 7)])
def test_recupera_nota_autorizada_do_documento_pago(app, tipo, situacao):
    doc, tentativa = _sem_vinculo(tipo)
    assinatura, iniciada_em = tentativa.assinatura, tentativa.iniciada_em
    with patch('app.services.tiny.pesquisar_notas_fiscais',
               return_value=[{'id': '911314754'}]) as pesquisar, \
            patch('app.services.tiny.obter_nota_fiscal',
                  return_value=_nota(situacao=situacao)) as obter:
        resultado = cobrancas_nf.sincronizar(doc)
    assert resultado['ok'] and resultado['autorizada']
    pesquisar.assert_called_once()
    obter.assert_called_once_with('911314754')
    db.session.refresh(doc)
    db.session.refresh(tentativa)
    assert doc.tiny_nota_fiscal_id == '911314754'
    assert int(doc.nf_numero) == 12693
    assert doc.nf_status == 'autorizada' and doc.nf_emitida_em is not None
    assert tentativa.estado == 'concluida' and tentativa.erro is None
    assert (tentativa.assinatura, tentativa.iniciada_em) == (assinatura, iniciada_em)
    assert Cobranca.query.count() == 0
    if tipo == 'venda':
        assert doc.parcelas[0].valor_pago == Decimal('606.00')
        assert doc.parcelas[0].forma_pagamento == 'pix'
    else:
        assert doc.status == 'paga' and doc.pago_em is not None


@pytest.mark.parametrize('alteracoes', [
    {'numero': '12694'},
    {'numero': ''},
    {'numero': None},
    {'serie': '2'},
    {'serie': None},
    {'tipo_nota': 'S'},
    {'tipo_nota': None},
    {'finalidade': '4'},
    {'finalidade': None},
    {'cliente': {'cpf_cnpj': '11222333000144'}},
    {'cliente': {}},
    {'cliente': None},
    {'valor_nota': '605.99'},
    {'valor_nota': None},
    {'valor_nota': 'inválido'},
    {'situacao': '2', 'descricao_situacao': 'Emitida'},
    {'situacao': '5', 'descricao_situacao': 'Rejeitada'},
    {'situacao': '10', 'descricao_situacao': 'Denegada'},
    {'situacao': '3', 'descricao_situacao': 'Cancelada'},
    {'situacao': None},
    {'id': '999999999'},
])
def test_detalhe_incompativel_ou_incompleto_preserva_erro_e_vinculo(app, alteracoes):
    doc, tentativa = _sem_vinculo()
    anterior = _estado(doc, tentativa)
    with patch('app.services.tiny.pesquisar_notas_fiscais',
               return_value=[{'id': '911314754'}]), \
            patch('app.services.tiny.obter_nota_fiscal', return_value=_nota(**alteracoes)):
        resultado = cobrancas_nf.sincronizar(doc)
    assert not resultado['ok'] and not resultado.get('autorizada')
    assert _estado(doc, tentativa) == anterior


def test_duas_notas_compativeis_nao_sao_escolhidas_por_ordem(app):
    doc, tentativa = _sem_vinculo()
    anterior = _estado(doc, tentativa)
    with patch('app.services.tiny.pesquisar_notas_fiscais', return_value=[
            {'id': '911314754'}, {'id': '911314755'}]), \
            patch('app.services.tiny.obter_nota_fiscal',
                  side_effect=lambda nota_id: _nota(id=nota_id)):
        resultado = cobrancas_nf.sincronizar(doc)
    assert not resultado['ok'] and not resultado.get('autorizada')
    assert _estado(doc, tentativa) == anterior


@pytest.mark.parametrize('itens', [
    [_item(codigo='SKU-OUTRO')],
    [_item(quantidade='2', valor_unitario='303.00')],
    [_item(quantidade='0.5', valor_unitario='1212.00')],
    [_item(quantidade='1', valor_unitario='605.99')],
    [_item(codigo=None)],
    [_item(quantidade=None)],
    [_item(valor_unitario=None)],
    [],
    None,
])
@pytest.mark.parametrize('tipo', ['venda', 'fatura'])
def test_mesmo_total_com_itens_divergentes_ou_incompletos_nao_recupera(app, tipo, itens):
    doc, tentativa = _sem_vinculo(tipo)
    anterior = _estado(doc, tentativa)
    with patch('app.services.tiny.pesquisar_notas_fiscais',
               return_value=[{'id': '911314754'}]), \
            patch('app.services.tiny.obter_nota_fiscal', return_value=_nota(itens=itens)):
        resultado = cobrancas_nf.sincronizar(doc)
    assert not resultado['ok'] and not resultado.get('autorizada')
    assert _estado(doc, tentativa) == anterior


@pytest.mark.parametrize('tipo', ['venda', 'fatura'])
def test_linhas_equivalentes_agregadas_por_sku_e_preco_recuperam_nota(app, tipo):
    doc, tentativa = _sem_vinculo(tipo)
    itens = [_item(quantidade='0.4'), _item(quantidade='0.6')]
    with patch('app.services.tiny.pesquisar_notas_fiscais',
               return_value=[{'id': '911314754'}]), \
            patch('app.services.tiny.obter_nota_fiscal', return_value=_nota(itens=itens)):
        resultado = cobrancas_nf.sincronizar(doc)
    assert resultado['ok'] and resultado['autorizada']
    db.session.refresh(doc)
    db.session.refresh(tentativa)
    assert doc.tiny_nota_fiscal_id == '911314754' and doc.nf_emitida_em is not None
    assert tentativa.estado == 'concluida' and tentativa.erro is None


def test_fatura_com_duas_vendas_confere_itens_consolidados(app):
    doc, tentativa = _sem_vinculo('fatura')
    primeira = doc.vendas[0]
    primeira.valor_total = primeira.itens[0].preco_unitario = Decimal('303.00')
    segunda = _venda(doc.cliente)
    segunda.valor_total = segunda.itens[0].preco_unitario = Decimal('303.00')
    segunda.itens[0].quantidade = 1
    tiny_nf.definir_sku('produto', segunda.itens[0].produto_id, 'SKU-NF-55', canal='b2b')
    doc.vendas.append(segunda)
    db.session.flush()
    tentativa.assinatura = cobrancas_nf.assinatura_documento(doc)
    db.session.commit()
    with patch('app.services.tiny.pesquisar_notas_fiscais',
               return_value=[{'id': '911314754'}]), \
            patch('app.services.tiny.obter_nota_fiscal', return_value=_nota(
                itens=[_item(quantidade='2', valor_unitario='303.00')])):
        resultado = cobrancas_nf.sincronizar(doc)
    assert resultado['ok'] and resultado['autorizada']
    db.session.refresh(doc)
    assert doc.tiny_nota_fiscal_id == '911314754' and doc.nf_emitida_em is not None


def test_item_local_sem_sku_mapeado_impede_recuperacao(app):
    doc, tentativa = _sem_vinculo()
    anterior = _estado(doc, tentativa)
    with patch('app.services.tiny_nf.sku_do_item', return_value=None), \
            patch('app.services.tiny.pesquisar_notas_fiscais',
                  return_value=[{'id': '911314754'}]), \
            patch('app.services.tiny.obter_nota_fiscal', return_value=_nota()):
        resultado = cobrancas_nf.sincronizar(doc)
    assert not resultado['ok'] and not resultado.get('autorizada')
    assert _estado(doc, tentativa) == anterior


@pytest.mark.parametrize('tipo_outro', ['venda', 'fatura'])
def test_nao_vincula_nota_ja_usada_por_outra_origem(app, tipo_outro):
    doc, tentativa = _sem_vinculo()
    outro, _ = _pendente(tipo_outro, cliente=doc.cliente, nota_id='911314754')
    anterior = _estado(doc, tentativa)
    with patch('app.services.tiny.pesquisar_notas_fiscais',
               return_value=[{'id': '911314754'}]), \
            patch('app.services.tiny.obter_nota_fiscal', return_value=_nota()):
        resultado = cobrancas_nf.sincronizar(doc)
    assert not resultado['ok'] and not resultado.get('autorizada')
    assert _estado(doc, tentativa) == anterior
    db.session.refresh(outro)
    assert outro.tiny_nota_fiscal_id == '911314754'


@pytest.mark.parametrize('alterar', ['cliente', 'item'])
def test_alteracao_local_apos_tentativa_impede_recuperacao(app, alterar):
    doc, tentativa = _sem_vinculo()
    if alterar == 'cliente':
        doc.cliente.cnpj_cpf = '11.222.333/0001-44'
    else:
        doc.itens[0].quantidade = 2
    db.session.commit()
    anterior = _estado(doc, tentativa)
    nf = _nota(cliente={'cpf_cnpj': doc.cliente.cnpj_cpf})
    with patch('app.services.tiny.pesquisar_notas_fiscais',
               return_value=[{'id': '911314754'}]), \
            patch('app.services.tiny.obter_nota_fiscal', return_value=nf):
        resultado = cobrancas_nf.sincronizar(doc)
    assert not resultado['ok'] and not resultado.get('autorizada')
    assert _estado(doc, tentativa) == anterior


def test_tentativa_legada_sem_assinatura_pode_recuperar_correspondencia_exata(app):
    doc, tentativa = _sem_vinculo()
    tentativa.assinatura = None
    db.session.commit()
    with patch('app.services.tiny.pesquisar_notas_fiscais',
               return_value=[{'id': '911314754'}]), \
            patch('app.services.tiny.obter_nota_fiscal', return_value=_nota()):
        resultado = cobrancas_nf.sincronizar(doc)
    assert resultado['ok'] and resultado['autorizada']
    db.session.refresh(tentativa)
    assert tentativa.assinatura is None


def test_recuperacao_manual_aceita_correspondencia_exata_sem_tentativa(app):
    doc, tentativa = _sem_vinculo()
    db.session.delete(tentativa)
    db.session.commit()
    with patch('app.services.tiny.pesquisar_notas_fiscais',
               return_value=[{'id': '911314754'}]), \
            patch('app.services.tiny.obter_nota_fiscal', return_value=_nota()):
        resultado = cobrancas_nf.sincronizar(doc)
    assert resultado['ok'] and resultado['autorizada']
    db.session.refresh(doc)
    assert doc.tiny_nota_fiscal_id == '911314754' and doc.nf_emitida_em is not None


@pytest.mark.parametrize('numero', [None, '', 'inválido'])
def test_documento_sem_numero_valido_nao_pesquisa_por_outros_dados(app, numero):
    doc, tentativa = _sem_vinculo()
    doc.nf_numero = numero
    db.session.commit()
    anterior = _estado(doc, tentativa)
    with patch('app.services.tiny.pesquisar_notas_fiscais') as pesquisar, \
            patch('app.services.tiny.obter_nota_fiscal') as obter:
        resultado = cobrancas_nf.sincronizar(doc)
    assert not resultado['ok'] and not resultado.get('autorizada')
    pesquisar.assert_not_called()
    obter.assert_not_called()
    assert _estado(doc, tentativa) == anterior


@pytest.mark.parametrize('falha', ['pesquisa', 'detalhe', 'sem_resultados', 'detalhe_vazio'])
def test_falha_no_gateway_preserva_diagnostico_de_duplicidade(app, falha):
    doc, tentativa = _sem_vinculo()
    anterior = _estado(doc, tentativa)
    with patch('app.services.tiny.pesquisar_notas_fiscais',
               return_value=[{'id': '911314754'}]) as pesquisar, \
            patch('app.services.tiny.obter_nota_fiscal', return_value=_nota()) as obter:
        if falha == 'pesquisa':
            pesquisar.side_effect = RuntimeError('Tiny indisponível')
        elif falha == 'detalhe':
            obter.side_effect = RuntimeError('Tiny indisponível')
        elif falha == 'sem_resultados':
            pesquisar.return_value = []
        else:
            obter.return_value = None
        resultado = cobrancas_nf.sincronizar(doc)
    assert not resultado['ok'] and not resultado.get('autorizada')
    assert _estado(doc, tentativa) == anterior


def test_serie_configurada_e_usada_para_identificar_nota(app):
    app.config['NF_SERIE'] = '2'
    doc, _ = _sem_vinculo()
    with patch('app.services.tiny.pesquisar_notas_fiscais',
               return_value=[{'id': '911314754'}]), \
            patch('app.services.tiny.obter_nota_fiscal', return_value=_nota(serie='2')):
        resultado = cobrancas_nf.sincronizar(doc)
    assert resultado['ok'] and resultado['autorizada']


@pytest.mark.parametrize('tipo', ['venda', 'fatura'])
def test_worker_recupera_tentativa_sem_id_e_ignora_legado_sem_tentativa(app, tipo):
    doc, tentativa = _sem_vinculo(tipo)
    legado, tentativa_legado = _sem_vinculo(tipo, cliente=doc.cliente)
    db.session.delete(tentativa_legado)
    db.session.commit()
    with patch('app.services.tiny.pesquisar_notas_fiscais',
               return_value=[{'id': '911314754'}]) as pesquisar, \
            patch('app.services.tiny.obter_nota_fiscal', return_value=_nota()) as obter:
        cobrancas_nf.sincronizar_pendentes(limite=5)
    pesquisar.assert_called_once()
    obter.assert_called_once_with('911314754')
    db.session.refresh(doc)
    db.session.refresh(tentativa)
    db.session.refresh(legado)
    assert doc.tiny_nota_fiscal_id == '911314754' and doc.nf_emitida_em is not None
    assert tentativa.estado == 'concluida' and tentativa.erro is None
    assert legado.tiny_nota_fiscal_id is None and legado.nf_emitida_em is None
    assert Cobranca.query.count() == 0

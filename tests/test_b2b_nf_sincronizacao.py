"""Reconciliação de notas corrigidas no Tiny, sem repetir atos fiscais."""
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import (
    AutomacaoCobranca,
    Cobranca,
    FaturaB2B,
    TentativaNFB2B,
    VendaB2BParcela,
)
from app.services import cobrancas_automacao, cobrancas_nf, tiny_nf_b2b
from app.services.cobrancas_trava import OperacaoEmAndamento, chave_documento
from tests.test_b2b_emitir_nf import _cliente_completo, _venda


def _pendente(tipo='venda', *, cliente=None, nota_id='911314754', paga=False):
    cliente = cliente or _cliente_completo()
    if tipo == 'fatura':
        doc = FaturaB2B(
            cliente_id=cliente.id, data_inicio=date(2026, 9, 1),
            data_fim=date(2026, 9, 18), vencimento=date(2026, 9, 20),
            valor_total=Decimal('100.00'), status='paga' if paga else 'fechada',
            pago_em=datetime(2026, 9, 19, 12) if paga else None,
        )
        db.session.add(doc)
        db.session.flush()
    else:
        doc = _venda(cliente)
        if paga:
            db.session.add(VendaB2BParcela(
                venda_id=doc.id, numero=1, vencimento=date(2026, 9, 20),
                valor=doc.valor_total, valor_pago=doc.valor_total,
                pago_em=datetime(2026, 9, 19, 12), forma_pagamento='pix',
            ))
    doc.tiny_nota_fiscal_id = nota_id
    doc.nf_numero = '012600'
    doc.nf_status = '5 rejeitada'
    tentativa = TentativaNFB2B(
        chave=chave_documento(doc), estado='conferir',
        erro='Inscrição Estadual do destinatário ausente ou incorreta',
        assinatura='a' * 64, iniciada_em=datetime(2026, 9, 18, 14),
    )
    db.session.add(tentativa)
    db.session.commit()
    return doc, tentativa


@pytest.mark.parametrize('tipo,situacao', [('venda', 6), ('fatura', '7')])
def test_sincroniza_documento_pago_sem_emitir_ou_cobrar(app, tipo, situacao):
    doc, tentativa = _pendente(tipo, paga=True)
    job = AutomacaoCobranca(
        chave=chave_documento(doc), tipo=tipo, documento_id=doc.id,
        referencia='Documento já pago', estado='erro',
        erro='Há pagamento total/parcial. Confira antes de cobrar.',
    )
    db.session.add(job)
    db.session.commit()
    assinatura, inicio = tentativa.assinatura, tentativa.iniciada_em
    erro_financeiro, atualizado = job.erro, job.atualizado_em
    retorno = {'id': 911314754, 'situacao': situacao, 'numero': '012693'}
    with patch('app.services.tiny.obter_nota_fiscal', return_value=retorno) as obter, \
            patch('app.services.tiny.incluir_nota_fiscal') as incluir, \
            patch('app.services.tiny.emitir_nota_fiscal') as emitir, \
            patch('app.services.tiny.contato_fiscal_por_documento') as contato, \
            patch('app.services.sicredi_cnab.gerar_remessa') as remessa, \
            patch('app.services.cobrancas_envio.enviar_automatico') as enviar:
        resultado = cobrancas_nf.sincronizar(doc)
        assert resultado['ok'] and resultado['autorizada']
        db.session.refresh(doc)
        confirmada_em = doc.nf_emitida_em
        assert confirmada_em is not None
        repeticao = cobrancas_nf.sincronizar(doc)
        assert repeticao['ok'] and repeticao['autorizada']
    assert obter.call_args_list[0].args == ('911314754',)
    for acao in (incluir, emitir, contato, remessa, enviar):
        acao.assert_not_called()
    db.session.refresh(doc)
    db.session.refresh(tentativa)
    db.session.refresh(job)
    assert doc.tiny_nota_fiscal_id == '911314754'
    assert doc.nf_status == 'autorizada'
    assert doc.nf_numero == '012693'
    assert doc.nf_emitida_em == confirmada_em
    assert tentativa.estado == 'concluida' and tentativa.erro is None
    assert tentativa.assinatura == assinatura and tentativa.iniciada_em == inicio
    assert (job.estado, job.erro, job.atualizado_em) == ('erro', erro_financeiro, atualizado)
    assert Cobranca.query.count() == 0
    if tipo == 'venda':
        assert doc.parcelas[0].valor_pago == Decimal('100.00')
        assert doc.parcelas[0].forma_pagamento == 'pix'
    else:
        assert doc.status == 'paga' and doc.pago_em == datetime(2026, 9, 19, 12)


@pytest.mark.parametrize('retorno', [None, {}])
def test_falha_de_consulta_preserva_nota_e_motivo_anterior(app, retorno):
    doc, tentativa = _pendente()
    anterior = (doc.nf_status, doc.nf_numero, tentativa.estado, tentativa.erro,
                tentativa.assinatura)
    with patch('app.services.tiny.obter_nota_fiscal', return_value=retorno), \
            patch('app.services.tiny.emitir_nota_fiscal') as emitir:
        resultado = cobrancas_nf.sincronizar(doc)
    assert not resultado['ok'] and not resultado.get('autorizada')
    emitir.assert_not_called()
    db.session.refresh(doc)
    db.session.refresh(tentativa)
    assert doc.tiny_nota_fiscal_id == '911314754' and doc.nf_emitida_em is None
    assert (doc.nf_status, doc.nf_numero, tentativa.estado, tentativa.erro,
            tentativa.assinatura) == anterior


def test_nota_retornada_com_outro_id_nao_atualiza_documento(app):
    doc, tentativa = _pendente()
    erro = tentativa.erro
    with patch('app.services.tiny.obter_nota_fiscal', return_value={
            'id': '999999999', 'situacao': 6, 'numero': '012693'}):
        resultado = cobrancas_nf.sincronizar(doc)
    assert not resultado['ok'] and not resultado.get('autorizada')
    db.session.refresh(doc)
    db.session.refresh(tentativa)
    assert doc.nf_emitida_em is None
    assert doc.nf_numero == '012600' and doc.nf_status == '5 rejeitada'
    assert doc.tiny_nota_fiscal_id == '911314754'
    assert tentativa.estado == 'conferir' and tentativa.erro == erro


@pytest.mark.parametrize('retorno', [
    {'situacao': 2, 'descricao_situacao': 'Emitida'},
    {'situacao': '2'},
    {'status_processamento': 3, 'status': 'OK'},
])
def test_emitida_ou_sucesso_da_api_nao_confirmam_autorizacao(app, retorno):
    doc, tentativa = _pendente()
    with patch('app.services.tiny.obter_nota_fiscal', return_value={
            'id': '911314754', **retorno}), \
            patch('app.services.tiny.emitir_nota_fiscal') as emitir, \
            patch('app.services.tiny.incluir_nota_fiscal') as incluir:
        resultado = cobrancas_nf.sincronizar(doc)
    assert not resultado.get('autorizada')
    db.session.refresh(doc)
    db.session.refresh(tentativa)
    assert doc.nf_emitida_em is None
    assert tentativa.estado == 'conferir'
    emitir.assert_not_called()
    incluir.assert_not_called()


def test_lote_rotaciona_pendencias_sem_esquecer_pagos_ou_faturas(app):
    cliente = _cliente_completo()
    docs = [_pendente(tipo, cliente=cliente, nota_id=f'{prefixo}{i}', paga=True)[0]
            for tipo, prefixo in [('venda', '100'), ('fatura', '200')]
            for i in range(3)]
    esperados = {doc.tiny_nota_fiscal_id for doc in docs}
    vistos = []

    def consultar(nota_id):
        vistos.append(str(nota_id))
        return {'id': str(nota_id), 'situacao': 5, 'descricao_situacao': 'Rejeitada'}

    with patch('app.services.tiny.obter_nota_fiscal', side_effect=consultar), \
            patch('app.services.tiny.incluir_nota_fiscal') as incluir, \
            patch('app.services.tiny.emitir_nota_fiscal') as emitir:
        cobrancas_nf.sincronizar_pendentes(limite=2)
        primeiro_lote = set(vistos)
        assert 0 < len(vistos) <= 4  # limite por modelo, conforme contrato
        assert len(primeiro_lote) == len(vistos)
        for _ in range(3):
            antes = len(vistos)
            cobrancas_nf.sincronizar_pendentes(limite=2)
            assert len(vistos) - antes <= 4
    assert set(vistos) == esperados
    assert any(nota not in primeiro_lote for nota in vistos)
    incluir.assert_not_called()
    emitir.assert_not_called()
    assert Cobranca.query.count() == 0


def test_worker_sincroniza_venda_paga_com_job_erro_sem_retomar_cobranca(app):
    doc, tentativa = _pendente(paga=True)
    job = AutomacaoCobranca(
        chave=chave_documento(doc), tipo='venda', documento_id=doc.id,
        referencia='Venda paga', estado='erro',
        erro='Há pagamento total/parcial. Confira antes de cobrar.',
    )
    db.session.add(job)
    db.session.commit()
    erro_financeiro, atualizado = job.erro, job.atualizado_em
    with patch('app.services.tiny.obter_nota_fiscal', return_value={
            'id': '911314754', 'situacao': 6, 'numero': '012693'}) as obter, \
            patch('app.services.cobrancas_automacao.processar') as processar, \
            patch('app.services.tiny.incluir_nota_fiscal') as incluir, \
            patch('app.services.tiny.emitir_nota_fiscal') as emitir, \
            patch('app.services.sicredi_cnab.gerar_remessa') as remessa, \
            patch('app.services.cobrancas_envio.enviar_automatico') as enviar:
        cobrancas_automacao.executar()
    obter.assert_called_once_with('911314754')
    for acao in (processar, incluir, emitir, remessa, enviar):
        acao.assert_not_called()
    db.session.refresh(doc)
    db.session.refresh(tentativa)
    db.session.refresh(job)
    assert doc.nf_status == 'autorizada' and doc.nf_emitida_em is not None
    assert doc.nf_numero == '012693'
    assert tentativa.estado == 'concluida' and tentativa.erro is None
    assert (job.estado, job.erro, job.atualizado_em) == ('erro', erro_financeiro, atualizado)
    assert doc.parcelas[0].valor_pago == Decimal('100.00')
    assert Cobranca.query.count() == 0


def test_excecao_na_consulta_preserva_nota_e_tentativa(app):
    doc, tentativa = _pendente()
    anterior = (doc.nf_status, doc.nf_numero, tentativa.estado, tentativa.erro,
                tentativa.assinatura)
    with patch('app.services.tiny.obter_nota_fiscal', side_effect=RuntimeError('indisponível')), \
            patch('app.services.tiny.incluir_nota_fiscal') as incluir, \
            patch('app.services.tiny.emitir_nota_fiscal') as emitir:
        resultado = cobrancas_nf.sincronizar(doc)
    assert not resultado['ok'] and not resultado.get('autorizada')
    incluir.assert_not_called()
    emitir.assert_not_called()
    db.session.refresh(doc)
    db.session.refresh(tentativa)
    assert doc.tiny_nota_fiscal_id == '911314754' and doc.nf_emitida_em is None
    assert (doc.nf_status, doc.nf_numero, tentativa.estado, tentativa.erro,
            tentativa.assinatura) == anterior


def test_documento_em_processamento_nao_consulta_tiny(app):
    doc, tentativa = _pendente()
    erro_anterior = tentativa.erro
    with patch('app.services.cobrancas_nf.trava',
               side_effect=OperacaoEmAndamento('Aguarde a operação atual.')), \
            patch('app.services.tiny.obter_nota_fiscal') as obter:
        resultado = cobrancas_nf.sincronizar(doc)
    assert not resultado['ok'] and not resultado.get('autorizada')
    obter.assert_not_called()
    db.session.refresh(doc)
    db.session.refresh(tentativa)
    assert doc.tiny_nota_fiscal_id == '911314754' and doc.nf_emitida_em is None
    assert doc.nf_status == '5 rejeitada'
    assert tentativa.estado == 'conferir' and tentativa.erro == erro_anterior


def test_consulta_nota_ja_emitida_atualiza_numero_e_preserva_confirmacao(app):
    doc, tentativa = _pendente()
    confirmada_em = datetime(2026, 9, 21, 16, 42)
    doc.nf_emitida_em = confirmada_em
    db.session.commit()
    assinatura = tentativa.assinatura
    with patch('app.services.tiny.obter_nota_fiscal', return_value={
            'id': '911314754', 'situacao': 6, 'numero': '012693'}), \
            patch('app.services.tiny.incluir_nota_fiscal') as incluir, \
            patch('app.services.tiny.emitir_nota_fiscal') as emitir:
        resultado = cobrancas_nf.sincronizar(doc)
    assert resultado['ok'] and resultado['autorizada']
    assert doc.nf_numero == '012693' and doc.nf_status == 'autorizada'
    assert doc.nf_emitida_em == confirmada_em
    assert doc.tiny_nota_fiscal_id == '911314754'
    assert tentativa.assinatura == assinatura and tentativa.erro is None
    incluir.assert_not_called()
    emitir.assert_not_called()


@pytest.mark.parametrize('situacao', [10, '10'])
def test_refazer_denegada_numerica_preserva_nota(app, situacao):
    doc, _ = _pendente()
    with patch('app.services.tiny.obter_nota_fiscal', return_value={
            'id': '911314754', 'situacao': situacao}), \
            patch('app.services.tiny.contato_fiscal_por_documento') as contato, \
            patch('app.services.tiny.incluir_nota_fiscal') as incluir, \
            patch('app.services.tiny.emitir_nota_fiscal') as emitir:
        resultado = tiny_nf_b2b.emitir_nf(doc, recriar=True)
    assert not resultado['ok']
    db.session.refresh(doc)
    assert doc.tiny_nota_fiscal_id == '911314754'
    assert doc.nf_emitida_em is None
    for acao in (contato, incluir, emitir):
        acao.assert_not_called()

"""Refazer mantém a referência anterior até confirmar a inclusão da nova NF."""
from datetime import date
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import FaturaB2B
from app.services import tiny_nf
from tests.test_b2b_emitir_nf import _cliente_completo, _venda


@pytest.fixture(params=['venda', 'fatura'])
def documento(request, app):
    cliente = _cliente_completo()
    if request.param == 'venda':
        doc = _venda(cliente)
    else:
        doc = FaturaB2B(cliente_id=cliente.id, data_inicio=date(2026, 9, 1),
                       data_fim=date(2026, 9, 19), vencimento=date(2026, 9, 21),
                       valor_total=100, status='fechada')
        db.session.add(doc)
    doc.tiny_nota_fiscal_id = '911314754'
    doc.nf_status = '5 rejeitada'
    doc.nf_numero = '012693'
    db.session.commit()
    return doc


def _referencia(doc):
    db.session.refresh(doc)
    return doc.tiny_nota_fiscal_id, doc.nf_status, doc.nf_numero, doc.nf_emitida_em


@pytest.mark.parametrize('segura', [False, True])
@pytest.mark.parametrize('resposta', [
    {'ok': False, 'erro': 'Registro em duplicidade - Nota fiscal já cadastrada'},
    {'ok': False, 'erro': 'Sem resposta do Tiny', 'incerto': True},
    {'ok': True},
])
def test_inclusao_sem_novo_id_preserva_referencia(documento, segura, resposta):
    anterior = _referencia(documento)

    def incluir(*args, **kwargs):
        # Mesmo enquanto o Tiny processa, outra consulta encontra a nota antiga.
        assert _referencia(documento) == anterior
        return resposta

    with patch('app.services.tiny.incluir_nota_fiscal', side_effect=incluir), \
            patch('app.services.tiny.emitir_nota_fiscal') as emitir:
        resultado = tiny_nf.emitir_nf_generico(
            documento, lambda: ({'tipo': 'S'}, None), recriar=True,
            inclusao_segura=segura)
    assert not resultado['ok']
    assert 'Falha ao criar a NF no Tiny' in resultado['msg']
    assert _referencia(documento) == anterior
    emitir.assert_not_called()


@pytest.mark.parametrize('segura', [False, True])
def test_excecao_na_inclusao_preserva_referencia(documento, segura):
    anterior = _referencia(documento)
    with patch('app.services.tiny.incluir_nota_fiscal',
               side_effect=TimeoutError('Tiny indisponível')), \
            patch('app.services.tiny.emitir_nota_fiscal') as emitir:
        with pytest.raises(TimeoutError):
            tiny_nf.emitir_nf_generico(
                documento, lambda: ({'tipo': 'S'}, None), recriar=True,
                inclusao_segura=segura)
    db.session.rollback()
    assert _referencia(documento) == anterior
    emitir.assert_not_called()


def test_payload_invalido_nao_apaga_referencia(documento):
    anterior = _referencia(documento)
    with patch('app.services.tiny.incluir_nota_fiscal') as incluir:
        resultado = tiny_nf.emitir_nf_generico(
            documento, lambda: (None, 'Cadastro incompleto'), recriar=True)
    assert not resultado['ok'] and resultado['msg'] == 'Cadastro incompleto'
    assert _referencia(documento) == anterior
    incluir.assert_not_called()


def test_emissao_rejeitada_mantem_nova_nota_confirmada(documento):
    anterior = _referencia(documento)

    def incluir(payload):
        assert _referencia(documento) == anterior
        return {'ok': True, 'id': '922222222', 'numero': '012700'}

    with patch('app.services.tiny.incluir_nota_fiscal', side_effect=incluir), \
            patch('app.services.tiny.emitir_nota_fiscal', return_value={
                'ok': False, 'status': 'rejeitada', 'erro': 'IE incorreta'}) as emitir, \
            patch('app.services.tiny.obter_nota_fiscal', return_value={
                'id': '922222222', 'situacao': '5', 'numero': '012700'}):
        resultado = tiny_nf.emitir_nf_generico(
            documento, lambda: ({'tipo': 'S'}, None), recriar=True)
    assert not resultado['ok']
    nota_id, status, numero, emitida_em = _referencia(documento)
    assert (nota_id, numero, emitida_em) == ('922222222', '012700', None)
    assert status.strip() == '5'
    emitir.assert_called_once_with('922222222')


def test_excecao_apos_inclusao_mantem_novo_id_sem_numero_antigo(documento):
    with patch('app.services.tiny.incluir_nota_fiscal', return_value={
                'ok': True, 'id': '922222222'}), \
            patch('app.services.tiny.emitir_nota_fiscal',
                  side_effect=TimeoutError('Sem confirmação')):
        with pytest.raises(TimeoutError):
            tiny_nf.emitir_nf_generico(
                documento, lambda: ({'tipo': 'S'}, None), recriar=True)
    db.session.rollback()
    assert _referencia(documento) == ('922222222', None, None, None)

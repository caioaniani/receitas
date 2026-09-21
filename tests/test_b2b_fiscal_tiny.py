from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import TentativaNFB2B
from app.services import tiny, tiny_nf_b2b
from tests.test_b2b_emitir_nf import _cliente_completo, _venda
from tests.test_central_cobrancas import _client


def _retornos(ie='123.456.789', documento='11222333000144'):
    return [
        {'status': 'OK', 'numero_paginas': 1, 'contatos': [
            {'contato': {'id': '123', 'cpf_cnpj': documento}}]},
        {'status': 'OK', 'contato': {'id': '123', 'cpf_cnpj': documento,
                                    'ie': ie, 'codigo': 'LAVO', 'situacao': 'Ativo'}},
    ]


@pytest.mark.parametrize('ie', ['123.456.789', 'ISENTO', ''])
def test_emissao_busca_ie_e_nao_sobrescreve_cadastro(app, ie):
    v = _venda(_cliente_completo(), sku='B2B')
    with patch('app.services.tiny._get', side_effect=_retornos(ie)) as get, \
            patch('app.services.tiny.incluir_nota_fiscal', return_value={'ok': True, 'id': 'nf'}) as inc, \
            patch('app.services.tiny.emitir_nota_fiscal', return_value={'ok': True, 'status': 'autorizada'}):
        assert tiny_nf_b2b.emitir_nf(v)['ok']
    cliente = inc.call_args.args[0]['cliente']
    assert cliente['ie'] == ie
    assert cliente['codigo'] == 'LAVO'
    assert cliente['atualizar_cliente'] == 'N'
    assert 'contribuinte' not in cliente  # Não inventa classificação fiscal.
    assert get.call_args_list[0].args[1]['cpf_cnpj'] == '11222333000144'


@pytest.mark.parametrize('retorno', [None, {'status': 'Erro', 'erros': [{'erro': 'Token inválido'}]},
                                    {'status': 'OK', 'contatos': []},
                                    {'status': 'OK', 'contatos': [
                                        {'contato': {'id': '1', 'cpf_cnpj': '11222333000144'}},
                                        {'contato': {'id': '2', 'cpf_cnpj': '11222333000144'}}]}])
def test_falha_consulta_nao_cria_nf_nem_trava_tentativa(app, retorno):
    v = _venda(_cliente_completo(), sku='B2B')
    with patch('app.services.tiny._get', return_value=retorno), \
            patch('app.services.tiny.incluir_nota_fiscal') as inc:
        assert not tiny_nf_b2b.emitir_nf(v)['ok']
    inc.assert_not_called()
    assert TentativaNFB2B.query.count() == 0


def test_consulta_confere_documento_no_detalhe_e_paginas(app):
    retornos = _retornos()
    retornos[0]['numero_paginas'] = 2
    retornos.insert(1, {'status': 'OK', 'numero_paginas': 2, 'contatos': []})
    retornos[-1]['contato']['cpf_cnpj'] = '99999999000199'
    with patch('app.services.tiny._get', side_effect=retornos) as get:
        with pytest.raises(ValueError, match='não corresponde'):
            tiny.contato_fiscal_por_documento('11.222.333/0001-44')
    assert get.call_args_list[1].args[1]['pagina'] == 2


@pytest.mark.parametrize('situacao', [None, {'situacao': 'processando'}, {'situacao': 'autorizada'},
                                    {'situacao': 'denegada'}])
def test_refazer_consulta_nota_e_preserva_quando_nao_rejeitada(app, situacao):
    v = _venda(_cliente_completo(), sku='B2B')
    v.tiny_nota_fiscal_id = 'anterior'
    db.session.commit()
    with patch('app.services.tiny.obter_nota_fiscal', return_value=situacao), \
            patch('app.services.tiny.incluir_nota_fiscal') as inc:
        resultado = tiny_nf_b2b.emitir_nf(v, recriar=True)
    inc.assert_not_called()
    assert v.tiny_nota_fiscal_id == 'anterior'
    assert resultado['ok'] == (situacao == {'situacao': 'autorizada'})


def test_refazer_rejeitada_busca_cadastro_atualizado(app):
    v = _venda(_cliente_completo(), sku='B2B')
    v.tiny_nota_fiscal_id = 'anterior'
    db.session.commit()
    with patch('app.services.tiny.obter_nota_fiscal', return_value={'situacao': 'rejeitada'}), \
            patch('app.services.tiny._get', side_effect=_retornos()), \
            patch('app.services.tiny.incluir_nota_fiscal', return_value={'ok': True, 'id': 'nova'}) as inc, \
            patch('app.services.tiny.emitir_nota_fiscal', return_value={'ok': True, 'status': 'autorizada'}):
        assert tiny_nf_b2b.emitir_nf(v, recriar=True)['ok']
    assert inc.call_args.args[0]['cliente']['ie'] == '123.456.789'
    assert v.tiny_nota_fiscal_id == 'nova'


def test_erro_certificado_permanece_no_detalhe(app, owner_user, contato_fiscal_tiny):
    v = _venda(_cliente_completo(), sku='B2B')
    with patch('app.services.tiny.incluir_nota_fiscal', return_value={'ok': True, 'id': 'nf'}), \
            patch('app.services.tiny.emitir_nota_fiscal', return_value={'ok': False, 'erro': 'Certificado expirado'}), \
            patch('app.services.tiny.obter_nota_fiscal', return_value={'situacao': 'rejeitada'}):
        assert not tiny_nf_b2b.emitir_nf(v)['ok']
        assert 'Certificado expirado' in tiny_nf_b2b.emitir_nf(v)['msg']
    resposta = _client(app, owner_user).get(f'/b2b/vendas/{v.id}')
    assert resposta.status_code == 200
    assert 'Certificado expirado' in resposta.get_data(as_text=True)

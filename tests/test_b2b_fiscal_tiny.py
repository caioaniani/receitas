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


# --- Caso FAT00003 (06/10/2026): zero × duplicado com mensagens distintas ---

def test_sem_cadastro_codigo_20_diz_que_nao_esta_cadastrado(app):
    with patch('app.services.tiny._get', return_value={
            'status': 'Erro', 'codigo_erro': '20',
            'erros': [{'erro': 'A consulta não retornou registros'}]}):
        with pytest.raises(ValueError) as exc:
            tiny.contato_fiscal_por_documento('11222333000144')
    msg = str(exc.value)
    assert 'não está cadastrado' in msg
    assert '11.222.333/0001-44' in msg
    assert 'duplicad' not in msg


def test_duplicados_listam_os_cadastros(app):
    retorno = {'status': 'OK', 'numero_paginas': 1, 'contatos': [
        {'contato': {'id': '1', 'nome': 'Zion A', 'codigo': 'Z1', 'cpf_cnpj': '11.222.333/0001-44'}},
        {'contato': {'id': '2', 'nome': 'Zion B', 'cpf_cnpj': '11222333000144'}}]}
    with patch('app.services.tiny._get', return_value=retorno):
        with pytest.raises(ValueError) as exc:
            tiny.contato_fiscal_por_documento('11222333000144')
    msg = str(exc.value)
    assert 'Há 2 cadastros ATIVOS' in msg
    assert '"Zion A" (id 1, código Z1)' in msg and '"Zion B" (id 2)' in msg


def test_documento_divergente_aparece_na_mensagem(app):
    retorno = {'status': 'OK', 'numero_paginas': 1, 'contatos': [
        {'contato': {'id': '9', 'nome': 'Outro', 'cpf_cnpj': '11222333000145'}}]}
    with patch('app.services.tiny._get', return_value=retorno):
        with pytest.raises(ValueError) as exc:
            tiny.contato_fiscal_por_documento('11222333000144')
    msg = str(exc.value)
    assert 'não está cadastrado' in msg
    assert '"Outro" (id 9) = 11222333000145' in msg


# --- Desempate de cadastro duplicado pelas notas já autorizadas (06/10/2026) ---

_DOC = '48884522000120'
_PESQ = {'status': 'OK', 'numero_paginas': 1, 'contatos': [
    {'contato': {'id': '1', 'nome': 'ZION SAO', 'cpf_cnpj': '48.884.522/0001-20'}},
    {'contato': {'id': '2', 'nome': 'ZION SÃO', 'cpf_cnpj': '48.884.522/0001-20'}}]}


def _det(cid, ie, numero, situacao='A'):
    return {'status': 'OK', 'contato': {'id': cid, 'cpf_cnpj': '48.884.522/0001-20', 'ie': ie,
                                        'numero': numero, 'cep': '05.630-100',
                                        'situacao': situacao, 'codigo': ''}}


def _ref(ie='ISENTO', numero='86'):
    return {'cpf_cnpj': '48.884.522/0001-20', 'ie': ie, 'numero': numero, 'cep': '05.630-100'}


def test_duplicado_desempata_pela_nota_anterior(app):
    # pesquisa, detalhe 1, detalhe 2 (desempate), detalhe final do escolhido
    seq = [_PESQ, _det('1', 'ISENTO', '86'), _det('2', '', '80'), _det('1', 'ISENTO', '86')]
    with patch('app.services.tiny._get', side_effect=seq):
        c = tiny.contato_fiscal_por_documento(_DOC, referencias=lambda: [_ref(), _ref()])
    assert c['id'] == '1' and c['ie'] == 'ISENTO'


@pytest.mark.parametrize('refs, trecho', [
    (None, 'não há notas anteriores'),
    (lambda: [], 'nenhuma nota autorizada'),
    (lambda: [_ref(), _ref(numero='80')], 'diferentes entre si'),
    (lambda: [_ref(ie='123')], '0 cadastros batem'),
])
def test_duplicado_sem_prova_segue_bloqueado(app, refs, trecho):
    seq = [_PESQ, _det('1', 'ISENTO', '86'), _det('2', '', '80')]
    with patch('app.services.tiny._get', side_effect=seq):
        with pytest.raises(ValueError) as exc:
            tiny.contato_fiscal_por_documento(_DOC, referencias=refs)
    assert 'Há 2 cadastros' in str(exc.value) and trecho in str(exc.value)


def test_duplicados_iguais_nas_duas_fichas_seguem_bloqueados(app):
    seq = [_PESQ, _det('1', 'ISENTO', '86'), _det('2', 'ISENTO', '86')]
    with patch('app.services.tiny._get', side_effect=seq):
        with pytest.raises(ValueError, match='2 cadastros batem'):
            tiny.contato_fiscal_por_documento(_DOC, referencias=lambda: [_ref()])


def test_emissao_da_fatura_usa_notas_autorizadas_do_cliente(app):
    from app.models import ClienteB2B, VendaB2B
    v = _venda(_cliente_completo(), sku='B2B')
    cli = db.session.get(ClienteB2B, v.cliente_id)
    cli.cnpj_cpf = _DOC
    antiga = VendaB2B(cliente_id=cli.id, valor_total=10, tiny_nota_fiscal_id='nf-antiga')
    db.session.add(antiga)
    db.session.commit()
    nf_antiga = {'situacao': '7', 'cliente': _ref()}
    seq = [_PESQ, _det('1', 'ISENTO', '86'), _det('2', '', '80'), _det('1', 'ISENTO', '86')]
    with patch('app.services.tiny._get', side_effect=seq), \
            patch('app.services.tiny.obter_nota_fiscal', return_value=nf_antiga), \
            patch('app.services.tiny.incluir_nota_fiscal', return_value={'ok': True, 'id': 'nf'}) as inc, \
            patch('app.services.tiny.emitir_nota_fiscal', return_value={'ok': True, 'status': 'autorizada'}):
        assert tiny_nf_b2b.emitir_nf(v)['ok']
    assert inc.call_args.args[0]['cliente']['ie'] == 'ISENTO'

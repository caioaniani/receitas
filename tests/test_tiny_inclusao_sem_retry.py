"""Inclusão de NF: resposta perdida não autoriza repetir uma operação externa."""
from unittest.mock import Mock

import pytest
import requests

from app.services import tiny


def _resposta(corpo=None, *, status=200, invalida=False):
    resposta = Mock(status_code=status)
    if invalida:
        resposta.json.side_effect = ValueError('JSON incompleto')
    else:
        resposta.json.return_value = corpo
    return resposta


def _sucesso():
    return _resposta({'retorno': {'status': 'OK', 'registros': {
        'registro': {'id': 'nf-908', 'numero': '11428'}}}})


@pytest.fixture
def api(app, monkeypatch):
    app.config['TINY_API_TOKEN'] = 'token-de-teste'
    post = Mock(return_value=_sucesso())
    pausa = Mock()
    monkeypatch.setattr(tiny.requests, 'post', post)
    monkeypatch.setattr(tiny.time, 'sleep', pausa)
    tiny._consumir_falha()
    return post, pausa


@pytest.mark.parametrize('falha', [requests.Timeout, requests.ConnectionError])
def test_resposta_perdida_nao_repete_inclusao(api, falha):
    post, pausa = api
    # A primeira chamada pode ter criado a NF no Tiny; uma segunda criaria
    # outra nota. O chamador deve receber a incerteza sem segunda chamada.
    post.side_effect = [falha('resposta perdida'), _sucesso()]
    resultado = tiny.incluir_nota_fiscal({'tipo': 'S'}, repetir_em_falha=False)
    assert resultado['ok'] is False and resultado['incerto'] is True
    assert resultado['erro']
    assert post.call_count == 1
    pausa.assert_not_called()


@pytest.mark.parametrize('status', [408, 500, 501, 502, 503, 504, 599])
def test_http_sem_confirmacao_nao_repete_e_fica_incerto(api, status):
    post, pausa = api
    post.side_effect = [_resposta(status=status), _sucesso()]
    resultado = tiny.incluir_nota_fiscal({'tipo': 'S'}, repetir_em_falha=False)
    assert resultado == {'ok': False, 'erro': f'HTTP {status}', 'incerto': True}
    assert post.call_count == 1
    pausa.assert_not_called()


@pytest.mark.parametrize('corpo', [
    None, [], {}, {'retorno': None}, {'retorno': []},
    {'retorno': {}}, {'retorno': {'status': 'processando'}},
    {'retorno': {'status': 'OK'}},
    {'retorno': {'status': 'OK', 'registros': {'registro': {'numero': '11428'}}}},
])
def test_resposta_sem_confirmacao_da_inclusao_fica_incerta(api, corpo):
    post, pausa = api
    post.return_value = _resposta(corpo)
    resultado = tiny.incluir_nota_fiscal({'tipo': 'S'}, repetir_em_falha=False)
    assert resultado['ok'] is False and resultado['incerto'] is True
    assert resultado['erro']
    assert post.call_count == 1
    pausa.assert_not_called()


def test_json_incompleto_fica_incerto(api):
    post, pausa = api
    post.return_value = _resposta(invalida=True)
    resultado = tiny.incluir_nota_fiscal({'tipo': 'S'}, repetir_em_falha=False)
    assert resultado == {'ok': False, 'erro': 'resposta nao-JSON', 'incerto': True}
    assert post.call_count == 1
    pausa.assert_not_called()


@pytest.mark.parametrize('status', [400, 401, 403, 404, 422, 429])
def test_rejeicao_http_explicita_e_falha_certa_sem_repetir(api, status):
    post, pausa = api
    post.return_value = _resposta(status=status)
    resultado = tiny.incluir_nota_fiscal({'tipo': 'S'}, repetir_em_falha=False)
    assert resultado == {'ok': False, 'erro': f'HTTP {status}', 'incerto': False}
    assert post.call_count == 1
    pausa.assert_not_called()


@pytest.mark.parametrize('detalhes', [
    {'erros': {'erro': 'natureza inválida'}},
    {'registros': {'registro': {'erros': [{'erro': 'natureza inválida'}]}}},
])
def test_rejeicao_tiny_conserva_motivo_e_permite_correcao(api, detalhes):
    post, pausa = api
    post.return_value = _resposta({'retorno': {'status': 'Erro', **detalhes}})
    resultado = tiny.incluir_nota_fiscal({'tipo': 'S'}, repetir_em_falha=False)
    assert resultado == {'ok': False, 'erro': 'natureza inválida', 'incerto': False}
    assert post.call_count == 1
    pausa.assert_not_called()


def test_token_ausente_e_falha_certa_sem_chamada(app, api):
    post, pausa = api
    app.config['TINY_API_TOKEN'] = ''
    resultado = tiny.incluir_nota_fiscal({'tipo': 'S'}, repetir_em_falha=False)
    assert resultado == {'ok': False, 'erro': 'TINY_API_TOKEN ausente', 'incerto': False}
    post.assert_not_called()
    pausa.assert_not_called()


def test_sucesso_sem_retry_preserva_id_e_numero(api):
    post, pausa = api
    resultado = tiny.incluir_nota_fiscal({'tipo': 'S'}, repetir_em_falha=False)
    assert resultado == {'ok': True, 'id': 'nf-908', 'numero': '11428'}
    assert post.call_count == 1
    pausa.assert_not_called()


def test_default_legado_ainda_repete_e_retorna_sucesso(api):
    post, pausa = api
    post.side_effect = [requests.Timeout('sem resposta'), _resposta(status=503), _sucesso()]
    resultado = tiny.incluir_nota_fiscal({'tipo': 'S'})
    assert resultado == {'ok': True, 'id': 'nf-908', 'numero': '11428'}
    assert post.call_count == 3
    assert [chamada.args[0] for chamada in pausa.call_args_list] == [1.0, 2.0]


def test_default_legado_preserva_falha_sem_novo_campo(api):
    post, pausa = api
    post.side_effect = requests.Timeout('sem resposta')
    resultado = tiny.incluir_nota_fiscal({'tipo': 'S'})
    assert resultado['ok'] is False and 'timeout' in resultado['erro']
    assert 'incerto' not in resultado
    assert post.call_count == 3


def test_get_sem_retorno_estruturado_preserva_none(api):
    post, pausa = api
    post.side_effect = requests.Timeout('sem resposta')
    assert tiny._get('nota.fiscal.incluir.php', repetir_em_falha=False) is None
    assert post.call_count == 1
    pausa.assert_not_called()

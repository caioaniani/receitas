"""Falhas reais do Sentry: conexão sem rota e corpo HTTP interrompido."""
from unittest.mock import Mock, patch

import pytest
import requests

from app.services import chatwoot


@pytest.fixture
def configurado(app):
    app.config.update(CHATWOOT_URL='https://atendimento.example',
                      CHATWOOT_API_TOKEN='teste', CHATWOOT_ACCOUNT_ID='1')
    return app


@pytest.mark.parametrize('erro', [requests.exceptions.ConnectionError,
                                requests.exceptions.ChunkedEncodingError,
                                requests.exceptions.Timeout])
@pytest.mark.parametrize('funcao', [chatwoot.listar_conversas,
                                  chatwoot.listar_conversas_paradas])
def test_recupera_leitura_sem_perder_conversas(configurado, erro, funcao):
    resposta = Mock(status_code=200, text='json')
    resposta.json.return_value = {'data': {'payload': [
        {'id': 19, 'last_activity_at': 1, 'meta': {'sender': {'name': 'Ana'}}}]}}
    with configurado.app_context(), patch.object(chatwoot.requests, 'get',
            side_effect=[erro('interrupção'), resposta]) as get, patch.object(chatwoot.time, 'sleep'):
        assert funcao(estrito=True)[0]['id'] == 19
        assert get.call_count == 2


def test_falha_definitiva_e_reportada(configurado, caplog):
    with configurado.app_context(), patch.object(chatwoot.requests, 'get',
            side_effect=requests.exceptions.ConnectionError('sem rota')) as get, patch.object(chatwoot.time, 'sleep'):
        with pytest.raises(chatwoot.ChatwootConsultaError):
            chatwoot.listar_conversas(estrito=True)
        assert get.call_count == 2
    assert any(r.levelname == 'ERROR' for r in caplog.records)


@pytest.mark.parametrize('status, chamadas', [(401, 1), (403, 1), (502, 2), (503, 2), (504, 2)])
def test_http_recuperavel_e_credencial_invalida(configurado, status, chamadas):
    with configurado.app_context(), patch.object(chatwoot.requests, 'get',
            return_value=Mock(status_code=status)) as get, patch.object(chatwoot.time, 'sleep'):
        with pytest.raises(chatwoot.ChatwootConsultaError):
            chatwoot.listar_conversas(estrito=True)
        assert get.call_count == chamadas


def test_resposta_vazia_valida_nao_e_falha(configurado):
    resposta = Mock(status_code=200, text='json')
    resposta.json.return_value = {'data': {'payload': []}}
    with configurado.app_context(), patch.object(chatwoot.requests, 'get', return_value=resposta):
        assert chatwoot.listar_conversas(estrito=True) == []


def test_consumidor_legado_mantem_contrato(configurado):
    with configurado.app_context(), patch.object(chatwoot.requests, 'get',
            side_effect=requests.exceptions.ConnectionError('sem rota')), patch.object(chatwoot.time, 'sleep'):
        assert chatwoot.listar_conversas_paradas() == []

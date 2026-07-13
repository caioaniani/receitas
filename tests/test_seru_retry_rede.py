"""Retry de rede no cliente Seru (12/07/2026, Sentry SSLEOFError): falha
transitória de conexão/SSL ganha até 2 novas tentativas; erro HTTP não.
"""
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.services import seru


def _resp(status=200, body=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body if body is not None else {'success': True}
    r.text = ''
    return r


def test_ssl_transitorio_re_tenta_e_sucede(app, monkeypatch):
    monkeypatch.setattr(seru, '_obter_token', lambda **kw: 'tok')
    monkeypatch.setattr(seru.time, 'sleep', lambda s: None)
    chamadas = []

    def _get_mock(*a, **kw):
        chamadas.append(1)
        if len(chamadas) < 3:
            raise requests.exceptions.SSLError('UNEXPECTED_EOF_WHILE_READING')
        return _resp()

    with patch.object(seru.requests, 'get', side_effect=_get_mock):
        out = seru._get('/orders')
    assert out == {'success': True}
    assert len(chamadas) == 3                       # 1 + 2 retries


def test_rede_persistente_estoura_depois_das_tentativas(app, monkeypatch):
    monkeypatch.setattr(seru, '_obter_token', lambda **kw: 'tok')
    monkeypatch.setattr(seru.time, 'sleep', lambda s: None)
    with patch.object(seru.requests, 'get',
                      side_effect=requests.exceptions.ConnectionError('down')):
        with pytest.raises(requests.exceptions.ConnectionError):
            seru._get('/orders')


def test_erro_http_nao_re_tenta(app, monkeypatch):
    monkeypatch.setattr(seru, '_obter_token', lambda **kw: 'tok')
    chamadas = []

    def _get_mock(*a, **kw):
        chamadas.append(1)
        return _resp(status=500)

    with patch.object(seru.requests, 'get', side_effect=_get_mock):
        with pytest.raises(RuntimeError):
            seru._get('/orders')
    assert len(chamadas) == 1                       # sem retry de HTTP


def test_502_de_gateway_re_tenta(app, monkeypatch):
    """502/503/504 são indisponibilidade transitória do gateway do Seru
    (Sentry 13/07/2026) — re-tenta como falha de rede; 4xx/500 não."""
    monkeypatch.setattr(seru, '_obter_token', lambda **kw: 'tok')
    monkeypatch.setattr(seru.time, 'sleep', lambda s: None)
    chamadas = []

    def _get_mock(*a, **kw):
        chamadas.append(1)
        return _resp(status=502) if len(chamadas) < 2 else _resp()

    with patch.object(seru.requests, 'get', side_effect=_get_mock):
        out = seru._get('/orders')
    assert out == {'success': True}
    assert len(chamadas) == 2


def test_404_nao_re_tenta(app, monkeypatch):
    monkeypatch.setattr(seru, '_obter_token', lambda **kw: 'tok')
    chamadas = []

    def _get_mock(*a, **kw):
        chamadas.append(1)
        return _resp(status=404)

    with patch.object(seru.requests, 'get', side_effect=_get_mock):
        with pytest.raises(RuntimeError):
            seru._get('/orders')
    assert len(chamadas) == 1

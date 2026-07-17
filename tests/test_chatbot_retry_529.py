"""Retry único de sobrecarga na chamada do bot (caso auditor 16/07/2026).

Um 529 pontual da Anthropic derrubava a conversa direto em handoff sem o
bot dizer nada (o max_retries=1 do SDK tinha sido consumido no mesmo pico).
`_chamar_com_retry_sobrecarga` re-tenta UMA vez (2s) em 429/500/529;
qualquer outro erro (ou segunda falha) sobe e o fallback pro humano segue.
"""
from unittest.mock import MagicMock, patch

import anthropic
import httpx
import pytest


def _err(status):
    resp = httpx.Response(
        status, request=httpx.Request('POST', 'https://api.anthropic.com'))
    return anthropic.APIStatusError('erro %d' % status, response=resp,
                                    body=None)


def _client(*efeitos):
    c = MagicMock()
    c.messages.create.side_effect = list(efeitos)
    return c


def test_529_retenta_uma_vez_e_devolve(app):
    from app.services.chatbot import _chamar_com_retry_sobrecarga
    ok = object()
    c = _client(_err(529), ok)
    with patch('time.sleep') as sl:
        assert _chamar_com_retry_sobrecarga(c, model='m') is ok
    assert c.messages.create.call_count == 2
    sl.assert_called_once_with(2)


def test_erro_nao_retryavel_sobe_direto(app):
    from app.services.chatbot import _chamar_com_retry_sobrecarga
    c = _client(_err(403), object())
    with patch('time.sleep'), pytest.raises(anthropic.APIStatusError):
        _chamar_com_retry_sobrecarga(c, model='m')
    assert c.messages.create.call_count == 1     # sem retry


def test_segunda_falha_sobe(app):
    """Dois 529 seguidos: a exceção sobe e o handoff de fallback do
    `responder` continua sendo o comportamento final."""
    from app.services.chatbot import _chamar_com_retry_sobrecarga
    c = _client(_err(529), _err(529))
    with patch('time.sleep'), pytest.raises(anthropic.APIStatusError):
        _chamar_com_retry_sobrecarga(c, model='m')
    assert c.messages.create.call_count == 2

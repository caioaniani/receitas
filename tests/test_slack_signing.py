"""verify_signing: HMAC-SHA256 + replay protection.

Regressao critica: se quebrar isso, qualquer um pode mandar eventos
falsos pro /slack/events e disparar acoes em nome de outros usuarios.
"""
import hashlib
import hmac
import time

import pytest


SIGNING_SECRET = 'test-signing-secret-abcdef123456'


@pytest.fixture
def app_with_signing(app):
    """App com signing secret configurado."""
    app.config['SLACK_SIGNING_SECRET'] = SIGNING_SECRET
    return app


def _make_signature(ts, body, secret=SIGNING_SECRET):
    base = f'v0:{ts}:{body}'.encode()
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return f'v0={digest}'


def test_assinatura_valida_aceita(app_with_signing):
    from app.services import slack
    ts = str(int(time.time()))
    body = '{"event":"test"}'
    sig = _make_signature(ts, body)
    with app_with_signing.app_context():
        ok = slack.verify_signing(
            {'X-Slack-Request-Timestamp': ts, 'X-Slack-Signature': sig},
            body,
        )
        assert ok is True


def test_assinatura_errada_rejeita(app_with_signing):
    from app.services import slack
    ts = str(int(time.time()))
    body = '{"event":"test"}'
    with app_with_signing.app_context():
        ok = slack.verify_signing(
            {'X-Slack-Request-Timestamp': ts,
             'X-Slack-Signature': 'v0=hash_falso_aqui'},
            body,
        )
        assert ok is False


def test_body_diferente_rejeita(app_with_signing):
    """Atacante intercepta assinatura mas muda body → rejeita."""
    from app.services import slack
    ts = str(int(time.time()))
    sig = _make_signature(ts, '{"event":"original"}')
    with app_with_signing.app_context():
        ok = slack.verify_signing(
            {'X-Slack-Request-Timestamp': ts, 'X-Slack-Signature': sig},
            '{"event":"alterado"}',  # body diferente do assinado
        )
        assert ok is False


def test_timestamp_antigo_rejeita(app_with_signing):
    """Replay attack: timestamp >5min de delta → rejeita."""
    from app.services import slack
    ts = str(int(time.time()) - 60 * 10)  # 10 minutos atras
    body = '{"event":"test"}'
    sig = _make_signature(ts, body)
    with app_with_signing.app_context():
        ok = slack.verify_signing(
            {'X-Slack-Request-Timestamp': ts, 'X-Slack-Signature': sig},
            body,
        )
        assert ok is False


def test_timestamp_futuro_distante_rejeita(app_with_signing):
    """Timestamp muito no futuro tambem cai na janela de replay."""
    from app.services import slack
    ts = str(int(time.time()) + 60 * 10)  # 10 minutos a frente
    body = '{"event":"test"}'
    sig = _make_signature(ts, body)
    with app_with_signing.app_context():
        ok = slack.verify_signing(
            {'X-Slack-Request-Timestamp': ts, 'X-Slack-Signature': sig},
            body,
        )
        assert ok is False


def test_headers_faltando_rejeita(app_with_signing):
    from app.services import slack
    with app_with_signing.app_context():
        # Sem timestamp
        assert slack.verify_signing({'X-Slack-Signature': 'v0=abc'}, 'body') is False
        # Sem signature
        assert slack.verify_signing({'X-Slack-Request-Timestamp': '123'}, 'body') is False
        # Ambos vazios
        assert slack.verify_signing({}, 'body') is False


def test_timestamp_nao_numerico_rejeita(app_with_signing):
    from app.services import slack
    with app_with_signing.app_context():
        ok = slack.verify_signing(
            {'X-Slack-Request-Timestamp': 'nao-numero',
             'X-Slack-Signature': 'v0=qualquer'},
            'body',
        )
        assert ok is False


def test_sem_secret_configurado_rejeita(app):
    """Se SLACK_SIGNING_SECRET nao tiver no env, recusa tudo."""
    from app.services import slack
    app.config['SLACK_SIGNING_SECRET'] = ''
    ts = str(int(time.time()))
    sig = _make_signature(ts, 'body')
    with app.app_context():
        ok = slack.verify_signing(
            {'X-Slack-Request-Timestamp': ts, 'X-Slack-Signature': sig},
            'body',
        )
        assert ok is False


def test_secret_diferente_rejeita(app_with_signing):
    """Assinatura feita com OUTRO secret nao bate."""
    from app.services import slack
    ts = str(int(time.time()))
    body = '{"event":"test"}'
    sig = _make_signature(ts, body, secret='outro-secret-malicioso')
    with app_with_signing.app_context():
        ok = slack.verify_signing(
            {'X-Slack-Request-Timestamp': ts, 'X-Slack-Signature': sig},
            body,
        )
        assert ok is False

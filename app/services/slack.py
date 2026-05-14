"""Cliente Slack — wrapper do slack-sdk + signing verification.

Tudo single-workspace: token bot fixo em SLACK_BOT_TOKEN.
"""
import hashlib
import hmac
import logging
import time

from flask import current_app

logger = logging.getLogger(__name__)


def _client():
    """Lazy import do WebClient pra nao quebrar se slack-sdk nao instalado."""
    from slack_sdk import WebClient
    token = (current_app.config.get('SLACK_BOT_TOKEN') or '').strip()
    if not token:
        raise RuntimeError('SLACK_BOT_TOKEN nao configurado.')
    return WebClient(token=token)


def disponivel():
    """True se bot esta configurado (token + signing)."""
    cfg = current_app.config
    return bool((cfg.get('SLACK_BOT_TOKEN') or '').strip()
                and (cfg.get('SLACK_SIGNING_SECRET') or '').strip())


def verify_signing(headers, body):
    """Verifica X-Slack-Signature contra HMAC-SHA256 do signing secret.

    Rejeita timestamps com >5min de delta (replay protection).
    Retorna True se valido.
    """
    secret = (current_app.config.get('SLACK_SIGNING_SECRET') or '').strip()
    if not secret:
        return False

    ts = headers.get('X-Slack-Request-Timestamp', '')
    sig = headers.get('X-Slack-Signature', '')
    if not ts or not sig:
        return False
    try:
        ts_int = int(ts)
    except ValueError:
        return False
    if abs(time.time() - ts_int) > 60 * 5:
        logger.warning('slack signing: timestamp fora da janela (replay)')
        return False

    base = f'v0:{ts}:{body}'.encode('utf-8')
    digest = hmac.new(secret.encode('utf-8'), base, hashlib.sha256).hexdigest()
    esperado = f'v0={digest}'
    return hmac.compare_digest(esperado, sig)


def post_message(channel, text=None, blocks=None, thread_ts=None):
    """chat.postMessage. Retorna {'ok': bool, 'ts': str, ...}."""
    try:
        kwargs = {'channel': channel}
        if text is not None:
            kwargs['text'] = text
        if blocks is not None:
            kwargs['blocks'] = blocks
        if thread_ts:
            kwargs['thread_ts'] = thread_ts
        resp = _client().chat_postMessage(**kwargs)
        return {'ok': True, 'ts': resp.get('ts'), 'channel': resp.get('channel')}
    except Exception as exc:  # noqa: BLE001
        logger.exception('slack post_message falhou')
        return {'ok': False, 'erro': str(exc)}


def update_message(channel, ts, text=None, blocks=None):
    """chat.update — substitui mensagem existente (apos clique de botao)."""
    try:
        kwargs = {'channel': channel, 'ts': ts}
        if text is not None:
            kwargs['text'] = text
        if blocks is not None:
            kwargs['blocks'] = blocks
        _client().chat_update(**kwargs)
        return {'ok': True}
    except Exception as exc:  # noqa: BLE001
        logger.exception('slack update_message falhou')
        return {'ok': False, 'erro': str(exc)}


def info_usuario(slack_user_id):
    """users.info. Retorna dict com nome, email, etc. None se falhar."""
    try:
        resp = _client().users_info(user=slack_user_id)
        return resp.get('user') or {}
    except Exception:
        logger.exception('slack info_usuario falhou')
        return None


def baixar_arquivo(file_info, tamanho_max=10 * 1024 * 1024):
    """Baixa um arquivo do Slack usando o bot token.

    `file_info`: dict do event['files'][i] (precisa de 'url_private_download',
    'mimetype', 'size', 'name').

    Retorna {'bytes': bytes, 'mimetype': str, 'name': str} ou None.
    Limita a `tamanho_max` (default 10MB) pra nao estourar memoria.
    """
    import requests
    url = file_info.get('url_private_download') or file_info.get('url_private')
    if not url:
        return None
    if (file_info.get('size') or 0) > tamanho_max:
        logger.warning('slack baixar_arquivo: arquivo grande demais (%s bytes)', file_info.get('size'))
        return None
    token = (current_app.config.get('SLACK_BOT_TOKEN') or '').strip()
    if not token:
        return None
    try:
        r = requests.get(url, headers={'Authorization': f'Bearer {token}'}, timeout=15)
        if r.status_code != 200:
            logger.warning('slack baixar_arquivo: %s %s', r.status_code, r.text[:200])
            return None
        return {
            'bytes': r.content,
            'mimetype': file_info.get('mimetype') or r.headers.get('Content-Type', ''),
            'name': file_info.get('name') or 'arquivo',
        }
    except Exception:
        logger.exception('slack baixar_arquivo falhou')
        return None

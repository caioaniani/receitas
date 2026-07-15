"""Spotify — controle remoto da música pela tela do padeiro (15/07/2026).

Pedido do dono: "integrar o Spotify à tela do /padeiro". Modo CONTROLE
REMOTO (Spotify Connect): a música toca no aparelho que já toca hoje
(celular/computador/caixa com Spotify aberto) e a tela do padeiro ganha os
controles — o que está tocando, pausar/pular, playlists e volume. O SERVIDOR
fala com a API do Spotify; o navegador só bate nas nossas rotas (token nunca
vai pro browser, CSP intocada).

Setup (uma vez):
1. Dono cria um app em https://developer.spotify.com/dashboard (conta da
   padaria) e cadastra a Redirect URI mostrada em /admin/spotify.
2. Envs no Railway: SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET (+ opcional
   SPOTIFY_REDIRECT_URI se quiser fixar).
3. Dono abre /admin/spotify e clica "Conectar" — loga com a conta da padaria
   UMA vez; o refresh token fica em AppConfig (sobrevive a deploy).

Controle de reprodução exige conta PREMIUM (limitação do Spotify) — os erros
403 PREMIUM_REQUIRED e 404 NO_ACTIVE_DEVICE viram mensagens claras pra tela.
Sem envs = rotas respondem "não configurado", nunca quebram (mesmo padrão do
BOT_API_TOKEN/CLAUDE_API_TOKEN).
"""
import base64
import logging
import time

import requests
from flask import current_app, url_for

from app.extensions import db
from app.models import AppConfig

logger = logging.getLogger(__name__)

_AUTH_BASE = 'https://accounts.spotify.com'
_API_BASE = 'https://api.spotify.com/v1'
_TIMEOUT = 10

# Chaves no AppConfig (runtime, sobrevivem a deploy; segredo de app fica em env).
_K_REFRESH = 'spotify_refresh_token'
_K_ACCESS = 'spotify_access_token'
_K_ACCESS_EXP = 'spotify_access_expira_em'   # epoch (str)
_K_CONTA = 'spotify_conta_display'           # nome da conta conectada (UI)

# Escopos: ler o player + controlar + listar playlists da conta.
SCOPES = ('user-read-playback-state user-modify-playback-state '
          'user-read-currently-playing playlist-read-private '
          'playlist-read-collaborative')


def _cfg():
    c = current_app.config
    return ((c.get('SPOTIFY_CLIENT_ID') or '').strip(),
            (c.get('SPOTIFY_CLIENT_SECRET') or '').strip())


def configurado():
    cid, sec = _cfg()
    return bool(cid and sec)


def conectado():
    return bool((AppConfig.get(_K_REFRESH) or '').strip())


def conta_display():
    return AppConfig.get(_K_CONTA) or ''


_CALLBACK_PROD = ('https://gestao.opaopadariaartesanal.com.br'
                  '/admin/spotify/callback')


def redirect_uri():
    """URI de callback EXATA que precisa estar cadastrada no app do Spotify.
    Env SPOTIFY_REDIRECT_URI manda; sem ela, deriva da URL do request atual;
    fora de request (troca de token em job/teste) cai na URL pública de prod
    — a URI enviada ao Spotify TEM que bater byte a byte com a cadastrada."""
    fixa = (current_app.config.get('SPOTIFY_REDIRECT_URI') or '').strip()
    if fixa:
        return fixa
    try:
        return url_for('main.spotify_callback', _external=True,
                       _scheme='https')
    except RuntimeError:
        return _CALLBACK_PROD


def url_autorizacao(state):
    """URL do consentimento do Spotify (o dono loga com a conta da padaria)."""
    from urllib.parse import urlencode
    cid, _ = _cfg()
    return f'{_AUTH_BASE}/authorize?' + urlencode({
        'client_id': cid,
        'response_type': 'code',
        'redirect_uri': redirect_uri(),
        'scope': SCOPES,
        'state': state,
    })


def _basic_auth_header():
    cid, sec = _cfg()
    raw = base64.b64encode(f'{cid}:{sec}'.encode()).decode()
    return {'Authorization': f'Basic {raw}'}


def trocar_codigo(code):
    """Troca o code do callback por tokens e persiste o refresh token.
    Retorna (ok, erro_str)."""
    try:
        r = requests.post(f'{_AUTH_BASE}/api/token', data={
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': redirect_uri(),
        }, headers=_basic_auth_header(), timeout=_TIMEOUT)
        if r.status_code != 200:
            return False, f'HTTP {r.status_code}: {r.text[:200]}'
        dados = r.json()
        refresh = dados.get('refresh_token')
        if not refresh:
            return False, 'resposta sem refresh_token'
        AppConfig.set(_K_REFRESH, refresh)
        AppConfig.set(_K_ACCESS, dados.get('access_token') or '')
        AppConfig.set(_K_ACCESS_EXP,
                      str(int(time.time()) + int(dados.get('expires_in') or 0)))
        db.session.commit()
        # Nome da conta conectada (best-effort, só pra UI do admin).
        try:
            me = requests.get(f'{_API_BASE}/me', headers={
                'Authorization': f'Bearer {dados.get("access_token")}'},
                timeout=_TIMEOUT)
            if me.status_code == 200:
                j = me.json()
                AppConfig.set(_K_CONTA,
                              j.get('display_name') or j.get('id') or '')
                db.session.commit()
        except Exception:  # noqa: BLE001 — só cosmético
            logger.exception('spotify: /me falhou (ignorado)')
        return True, None
    except Exception as exc:  # noqa: BLE001
        logger.exception('spotify: troca de código falhou')
        return False, str(exc)


def desconectar():
    for k in (_K_REFRESH, _K_ACCESS, _K_ACCESS_EXP, _K_CONTA):
        AppConfig.set(k, '')
    db.session.commit()


def _access_token(forcar_refresh=False):
    """Access token válido, renovando pelo refresh token quando vence.
    None = não conectado ou refresh falhou."""
    refresh = (AppConfig.get(_K_REFRESH) or '').strip()
    if not refresh:
        return None
    if not forcar_refresh:
        exp = AppConfig.get(_K_ACCESS_EXP) or '0'
        tok = (AppConfig.get(_K_ACCESS) or '').strip()
        try:
            valido = tok and (int(float(exp)) - 60) > int(time.time())
        except ValueError:
            valido = False
        if valido:
            return tok
    try:
        r = requests.post(f'{_AUTH_BASE}/api/token', data={
            'grant_type': 'refresh_token',
            'refresh_token': refresh,
        }, headers=_basic_auth_header(), timeout=_TIMEOUT)
        if r.status_code != 200:
            logger.warning('spotify: refresh falhou HTTP %s: %s',
                           r.status_code, r.text[:200])
            return None
        dados = r.json()
        tok = dados.get('access_token') or ''
        AppConfig.set(_K_ACCESS, tok)
        AppConfig.set(_K_ACCESS_EXP,
                      str(int(time.time()) + int(dados.get('expires_in') or 0)))
        # O Spotify PODE rotacionar o refresh token — persistir quando vier.
        if dados.get('refresh_token'):
            AppConfig.set(_K_REFRESH, dados['refresh_token'])
        db.session.commit()
        return tok or None
    except Exception:  # noqa: BLE001
        logger.exception('spotify: refresh de token explodiu')
        return None


def _req(metodo, caminho, *, params=None, json_body=None, _retry=True):
    """Chamada autenticada à API. Retorna (status_code, dict|None, erro_str).
    401 renova o token e tenta 1x de novo."""
    tok = _access_token()
    if not tok:
        return 0, None, 'nao_conectado'
    try:
        r = requests.request(metodo, f'{_API_BASE}{caminho}', params=params,
                             json=json_body,
                             headers={'Authorization': f'Bearer {tok}'},
                             timeout=_TIMEOUT)
        if r.status_code == 401 and _retry:
            tok2 = _access_token(forcar_refresh=True)
            if tok2:
                return _req(metodo, caminho, params=params,
                            json_body=json_body, _retry=False)
        corpo = None
        if r.text:
            try:
                corpo = r.json()
            except ValueError:
                corpo = None
        return r.status_code, corpo, None
    except Exception as exc:  # noqa: BLE001
        logger.exception('spotify: %s %s falhou', metodo, caminho)
        return 0, None, str(exc)


def _erro_humano(status, corpo):
    """Erro da API → mensagem que o padeiro entende (sem vazar detalhe)."""
    reason = ((corpo or {}).get('error') or {}).get('reason') or ''
    msg = ((corpo or {}).get('error') or {}).get('message') or ''
    if status == 0:
        return 'sem ligação com o Spotify (rede)'
    if reason == 'PREMIUM_REQUIRED' or 'premium' in msg.lower():
        return 'a conta do Spotify precisa ser Premium pra controlar a música'
    if reason == 'NO_ACTIVE_DEVICE' or status == 404:
        return ('nenhum aparelho tocando — abra o Spotify no aparelho do som '
                'e dê play uma vez')
    if status == 429:
        return 'o Spotify pediu calma (muitos comandos) — tente em instantes'
    return f'o Spotify recusou o comando (HTTP {status})'


def estado_player():
    """Estado pro widget: o que toca, se está pausado, volume, aparelho.
    Sempre retorna dict com 'ok' (nunca levanta)."""
    if not configurado():
        return {'ok': False, 'motivo': 'nao_configurado'}
    if not conectado():
        return {'ok': False, 'motivo': 'nao_conectado'}
    status, corpo, erro = _req('GET', '/me/player')
    if erro == 'nao_conectado':
        return {'ok': False, 'motivo': 'nao_conectado'}
    if status == 204 or (status == 200 and not corpo):
        return {'ok': True, 'tocando': False, 'sem_aparelho': True,
                'mensagem': ('nenhum aparelho ativo — abra o Spotify no '
                             'aparelho do som e dê play uma vez')}
    if status != 200:
        return {'ok': False, 'motivo': 'erro',
                'mensagem': _erro_humano(status, corpo)}
    item = (corpo.get('item') or {})
    artistas = ', '.join(a.get('name') or '' for a in item.get('artists') or [])
    dev = corpo.get('device') or {}
    return {
        'ok': True,
        'tocando': bool(corpo.get('is_playing')),
        'sem_aparelho': False,
        'musica': item.get('name') or '—',
        'artista': artistas,
        'capa': next((i.get('url') for i in
                      ((item.get('album') or {}).get('images') or [])), None),
        'aparelho': dev.get('name') or '',
        'volume': dev.get('volume_percent'),
        'volume_controlavel': not dev.get('supports_volume') is False,
        'contexto_uri': (corpo.get('context') or {}).get('uri'),
    }


def listar_playlists(limite=30):
    """Playlists da conta conectada (nome + uri), pro seletor da tela."""
    status, corpo, _ = _req('GET', '/me/playlists',
                            params={'limit': max(1, min(int(limite), 50))})
    if status != 200 or not corpo:
        return []
    return [{'nome': p.get('name') or '?', 'uri': p.get('uri'),
             'total': (p.get('tracks') or {}).get('total')}
            for p in corpo.get('items') or [] if p and p.get('uri')]


def executar_acao(acao, valor=None):
    """Executa um comando do widget. Retorna (ok, mensagem_erro|None).
    Ações: play, pause, next, previous, volume (0-100), playlist (uri)."""
    if not configurado():
        return False, 'Spotify não configurado'
    rotas = {
        'play': ('PUT', '/me/player/play', None),
        'pause': ('PUT', '/me/player/pause', None),
        'next': ('POST', '/me/player/next', None),
        'previous': ('POST', '/me/player/previous', None),
    }
    if acao in rotas:
        metodo, caminho, body = rotas[acao]
        status, corpo, erro = _req(metodo, caminho, json_body=body)
    elif acao == 'volume':
        try:
            vol = max(0, min(int(valor), 100))
        except (TypeError, ValueError):
            return False, 'volume inválido'
        status, corpo, erro = _req('PUT', '/me/player/volume',
                                   params={'volume_percent': vol})
    elif acao == 'playlist':
        uri = (valor or '').strip()
        if not uri.startswith('spotify:'):
            return False, 'playlist inválida'
        status, corpo, erro = _req('PUT', '/me/player/play',
                                   json_body={'context_uri': uri})
    else:
        return False, 'ação desconhecida'
    if erro == 'nao_conectado':
        return False, 'Spotify não conectado — peça ao administrador'
    if status in (200, 202, 204):
        return True, None
    return False, _erro_humano(status, corpo)

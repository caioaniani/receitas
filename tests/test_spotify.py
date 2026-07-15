"""Integração Spotify — widget 🎵 da tela do padeiro (15/07/2026).

Modo controle remoto (Spotify Connect): o servidor fala com a API do Spotify
com tokens guardados em AppConfig; a tela do padeiro só bate nas nossas
rotas. A API do Spotify é SEMPRE mockada aqui (mesmo padrão da Anthropic nos
testes de IA) — nenhum teste sai pra rede.
"""
from unittest.mock import patch

from app.extensions import db
from app.models import AppConfig, Usuario


class _Resp:
    def __init__(self, status=200, corpo=None, texto=None):
        self.status_code = status
        self._corpo = corpo
        self.text = texto if texto is not None else ('x' if corpo else '')

    def json(self):
        if self._corpo is None:
            raise ValueError('sem json')
        return self._corpo


def _cfg(app):
    app.config['SPOTIFY_CLIENT_ID'] = 'cid-teste'
    app.config['SPOTIFY_CLIENT_SECRET'] = 'sec-teste'


def _conecta(app):
    """Simula conta conectada com access token ainda válido."""
    import time
    AppConfig.set('spotify_refresh_token', 'refresh-abc')
    AppConfig.set('spotify_access_token', 'access-abc')
    AppConfig.set('spotify_access_expira_em', str(int(time.time()) + 3600))
    db.session.commit()


def _padeiro(login='spotpad'):
    u = Usuario(nome='Padeiro Spot', login=login, papel='padeiro')
    u.set_senha('12345678')
    db.session.add(u)
    db.session.commit()
    return u


def _login(c, login, senha='12345678'):
    return c.post('/auth/login', data={'login': login, 'senha': senha})


def test_config_mapeia_as_envs_spotify():
    """Regressão do bug do primeiro deploy: o Flask NÃO absorve env var
    sozinho — cada uma precisa estar declarada no config.py. As SPOTIFY_*
    ficaram de fora e o app nunca via as variáveis do Railway (badge
    'faltando' mesmo com tudo certo lá)."""
    from config import Config
    for attr in ('SPOTIFY_CLIENT_ID', 'SPOTIFY_CLIENT_SECRET',
                 'SPOTIFY_REDIRECT_URI'):
        assert hasattr(Config, attr), f'config.py não mapeia {attr}'


# ── Serviço ──────────────────────────────────────────────────────────────

def test_estado_sem_config_e_sem_conexao(app):
    from app.services import spotify
    with app.app_context():
        assert spotify.estado_player() == {'ok': False,
                                           'motivo': 'nao_configurado'}
        _cfg(app)
        assert spotify.estado_player() == {'ok': False,
                                           'motivo': 'nao_conectado'}


def test_trocar_codigo_persiste_refresh_token(app):
    from app.services import spotify
    with app.app_context():
        _cfg(app)
        token_resp = _Resp(200, {'access_token': 'ac1', 'refresh_token': 'rf1',
                                 'expires_in': 3600})
        me_resp = _Resp(200, {'display_name': 'Padaria Opão'})
        with patch('app.services.spotify.requests.post',
                   return_value=token_resp), \
             patch('app.services.spotify.requests.get', return_value=me_resp):
            ok, erro = spotify.trocar_codigo('code-xyz')
        assert ok and erro is None
        assert AppConfig.get('spotify_refresh_token') == 'rf1'
        assert spotify.conta_display() == 'Padaria Opão'


def test_estado_204_vira_sem_aparelho(app):
    from app.services import spotify
    with app.app_context():
        _cfg(app)
        _conecta(app)
        with patch('app.services.spotify.requests.request',
                   return_value=_Resp(204)):
            est = spotify.estado_player()
        assert est['ok'] and est['sem_aparelho'] is True
        assert 'aparelho' in est['mensagem']


def test_estado_tocando_mapeia_musica_e_volume(app):
    from app.services import spotify
    corpo = {
        'is_playing': True,
        'item': {'name': 'Bohemian Rhapsody',
                 'artists': [{'name': 'Queen'}],
                 'album': {'images': [{'url': 'http://img/capa.jpg'}]}},
        'device': {'name': 'Som da Padaria', 'volume_percent': 55},
        'context': {'uri': 'spotify:playlist:pl1'},
    }
    with app.app_context():
        _cfg(app)
        _conecta(app)
        with patch('app.services.spotify.requests.request',
                   return_value=_Resp(200, corpo, texto='j')):
            est = spotify.estado_player()
        assert est['tocando'] is True
        assert est['musica'] == 'Bohemian Rhapsody'
        assert est['artista'] == 'Queen'
        assert est['aparelho'] == 'Som da Padaria'
        assert est['volume'] == 55


def test_acao_pause_chama_api_certa(app):
    from app.services import spotify
    with app.app_context():
        _cfg(app)
        _conecta(app)
        with patch('app.services.spotify.requests.request',
                   return_value=_Resp(204)) as req:
            ok, erro = spotify.executar_acao('pause')
        assert ok and erro is None
        args, kwargs = req.call_args
        assert args[0] == 'PUT'
        assert args[1].endswith('/me/player/pause')
        assert kwargs['headers']['Authorization'] == 'Bearer access-abc'


def test_acao_sem_aparelho_da_mensagem_humana(app):
    from app.services import spotify
    corpo = {'error': {'status': 404, 'reason': 'NO_ACTIVE_DEVICE',
                       'message': 'Player command failed'}}
    with app.app_context():
        _cfg(app)
        _conecta(app)
        with patch('app.services.spotify.requests.request',
                   return_value=_Resp(404, corpo, texto='j')):
            ok, erro = spotify.executar_acao('next')
        assert not ok
        assert 'aparelho' in erro


def test_acao_premium_required_da_mensagem_humana(app):
    from app.services import spotify
    corpo = {'error': {'status': 403, 'reason': 'PREMIUM_REQUIRED',
                       'message': 'Premium required'}}
    with app.app_context():
        _cfg(app)
        _conecta(app)
        with patch('app.services.spotify.requests.request',
                   return_value=_Resp(403, corpo, texto='j')):
            ok, erro = spotify.executar_acao('pause')
        assert not ok
        assert 'Premium' in erro


def test_401_renova_token_e_repete_uma_vez(app):
    from app.services import spotify
    with app.app_context():
        _cfg(app)
        _conecta(app)
        respostas = [_Resp(401, {'error': {'status': 401}}, texto='j'),
                     _Resp(204)]
        refresh_resp = _Resp(200, {'access_token': 'ac-novo',
                                   'expires_in': 3600})
        with patch('app.services.spotify.requests.request',
                   side_effect=respostas) as req, \
             patch('app.services.spotify.requests.post',
                   return_value=refresh_resp):
            ok, _ = spotify.executar_acao('pause')
        assert ok
        assert req.call_count == 2
        assert AppConfig.get('spotify_access_token') == 'ac-novo'


def test_acao_playlist_valida_uri(app):
    from app.services import spotify
    with app.app_context():
        _cfg(app)
        _conecta(app)
        ok, erro = spotify.executar_acao('playlist', 'https://malicioso')
        assert not ok and 'inválida' in erro


# ── Endpoints do padeiro ─────────────────────────────────────────────────

def test_endpoint_estado_exige_papel_padeiro(app, loja):
    with app.app_context():
        u = Usuario(nome='Func', login='spotfunc', papel='funcionario',
                    loja_id=loja.id)
        u.set_senha('12345678')
        db.session.add(u)
        db.session.commit()
    c = app.test_client()
    _login(c, 'spotfunc')
    assert c.get('/padeiro/spotify/estado').status_code == 403


def test_endpoint_estado_nao_configurado(app):
    with app.app_context():
        _padeiro('spotpad1')
    c = app.test_client()
    _login(c, 'spotpad1')
    r = c.get('/padeiro/spotify/estado')
    assert r.status_code == 200
    assert r.get_json() == {'ok': False, 'motivo': 'nao_configurado'}


def test_endpoint_acao_devolve_erro_do_servico(app):
    with app.app_context():
        _padeiro('spotpad2')
    c = app.test_client()
    _login(c, 'spotpad2')
    with patch('app.services.spotify.executar_acao',
               return_value=(False, 'a conta do Spotify precisa ser Premium '
                                    'pra controlar a música')) as ex:
        r = c.post('/padeiro/spotify/acao', json={'acao': 'pause'})
    assert r.status_code == 422
    assert 'Premium' in r.get_json()['erro']
    ex.assert_called_once_with('pause', None, device_id=None)


def test_endpoint_acao_ok(app):
    with app.app_context():
        _padeiro('spotpad3')
    c = app.test_client()
    _login(c, 'spotpad3')
    with patch('app.services.spotify.executar_acao',
               return_value=(True, None)):
        r = c.post('/padeiro/spotify/acao',
                   json={'acao': 'volume', 'valor': 40})
    assert r.status_code == 200
    assert r.get_json()['ok'] is True


def test_escopo_inclui_streaming_para_tocar_na_tela():
    """Tocar NA tela (Web Playback SDK) exige o escopo 'streaming' — sem ele
    o player do navegador recusa o token (decisão do dono 15/07/2026)."""
    from app.services import spotify
    for s in ('streaming', 'user-read-email', 'user-read-private'):
        assert s in spotify.SCOPES


def test_acao_transferir_faz_put_me_player(app):
    from app.services import spotify
    with app.app_context():
        _cfg(app)
        _conecta(app)
        with patch('app.services.spotify.requests.request',
                   return_value=_Resp(204)) as req:
            ok, erro = spotify.executar_acao('transferir', 'dev-tela-123')
        assert ok and erro is None
        args, kwargs = req.call_args
        assert args[0] == 'PUT' and args[1].endswith('/me/player')
        assert kwargs['json']['device_ids'] == ['dev-tela-123']
        assert kwargs['json']['play'] is True


def test_acao_playlist_mira_device_id(app):
    from app.services import spotify
    with app.app_context():
        _cfg(app)
        _conecta(app)
        with patch('app.services.spotify.requests.request',
                   return_value=_Resp(204)) as req:
            ok, _ = spotify.executar_acao('playlist', 'spotify:playlist:p1',
                                          device_id='dev-tela-123')
        assert ok
        _, kwargs = req.call_args
        assert kwargs['params'] == {'device_id': 'dev-tela-123'}
        assert kwargs['json'] == {'context_uri': 'spotify:playlist:p1'}


def test_endpoint_token_do_player(app):
    """A tela-player busca o token aqui (papel padeiro obrigatório); sem
    conexão devolve 503, conectado devolve o token + validade."""
    with app.app_context():
        _padeiro('spotpad4')
    c = app.test_client()
    _login(c, 'spotpad4')
    assert c.get('/padeiro/spotify/token').status_code == 503
    with c.application.app_context():
        _cfg(c.application)
        _conecta(c.application)
    r = c.get('/padeiro/spotify/token')
    assert r.status_code == 200
    d = r.get_json()
    assert d['ok'] and d['access_token'] == 'access-abc'
    assert d['expira_em_s'] > 0


def test_endpoint_token_exige_papel(app, loja):
    with app.app_context():
        u = Usuario(nome='Func', login='spotfunc2', papel='funcionario',
                    loja_id=loja.id)
        u.set_senha('12345678')
        db.session.add(u)
        db.session.commit()
    c = app.test_client()
    _login(c, 'spotfunc2')
    assert c.get('/padeiro/spotify/token').status_code == 403


def test_csp_report_guarda_violacao(app):
    """O report-uri da tela do padeiro grava a violação (host de áudio
    bloqueado) em AppConfig — é assim que se descobre qual CDN faltou
    liberar quando a música morre em ~10s."""
    import json
    c = app.test_client()
    r = c.post('/padeiro/csp-report',
               data=json.dumps({'csp-report': {
                   'violated-directive': 'media-src',
                   'blocked-uri': 'https://audio-novo.cdnexotico.net/x'}}),
               content_type='application/csp-report')
    assert r.status_code == 204
    with app.app_context():
        salvos = json.loads(AppConfig.get('padeiro_csp_reports') or '[]')
        assert salvos and salvos[-1]['diretiva'] == 'media-src'
        assert 'cdnexotico' in salvos[-1]['bloqueado']


def test_csp_do_padeiro_libera_spotify(app, admin_user):
    """A CSP da tela do padeiro precisa liberar o SDK (sdk.scdn.co) e o
    áudio (blob:/spotifycdn) — e SÓ nela; o resto do app segue estrito."""
    c = app.test_client()
    _login(c, 'admin', '123')
    csp_padeiro = c.get('/padeiro/').headers.get('Content-Security-Policy', '')
    assert 'sdk.scdn.co' in csp_padeiro
    assert 'blob:' in csp_padeiro
    csp_resto = c.get('/pedidos/').headers.get('Content-Security-Policy', '')
    assert 'sdk.scdn.co' not in csp_resto


# ── Admin (conexão da conta) ─────────────────────────────────────────────

def test_admin_spotify_tela_e_callback_state(app, admin_user):
    c = app.test_client()
    _login(c, 'admin', '123')
    # Tela de status abre e mostra o passo a passo.
    body = c.get('/admin/spotify').get_data(as_text=True)
    assert 'developer.spotify.com' in body
    # Callback com state errado NÃO troca código.
    with patch('app.services.spotify.trocar_codigo') as tc:
        r = c.get('/admin/spotify/callback?code=x&state=forjado',
                  follow_redirects=False)
    assert r.status_code == 302
    tc.assert_not_called()


def test_admin_conectar_sem_env_avisa(app, admin_user):
    c = app.test_client()
    _login(c, 'admin', '123')
    r = c.get('/admin/spotify/conectar', follow_redirects=True)
    assert 'SPOTIFY_CLIENT_ID' in r.get_data(as_text=True)

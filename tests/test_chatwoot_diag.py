"""Diagnostico do Chatwoot (/admin/debug-chatwoot + chatwoot.diagnostico).

Criado no incidente de 12/06/2026: WhatsApp "Falha ao enviar" + Instagram
"400 Session Invalid" + app dos atendentes com "unexpected error". A rota
roda as checagens DO SERVIDOR de prod e conclui sozinha qual familia de
problema e — hospedagem, token nosso, ou canais Meta."""
from unittest.mock import patch


class _Resp:
    def __init__(self, status_code=200, body=None, text=''):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError('no json')
        return self._body


def _cfg(app, **extra):
    app.config['CHATWOOT_URL'] = 'https://chat.exemplo.com.br'
    app.config['CHATWOOT_ACCOUNT_ID'] = '1'
    app.config['CHATWOOT_API_TOKEN'] = 'tok-api'
    app.config['CHATWOOT_BOT_TOKEN'] = 'tok-bot'
    for k, v in extra.items():
        app.config[k] = v


def test_sem_url_configura_conclusao_de_env(app):
    from app.services import chatwoot
    app.config['CHATWOOT_URL'] = ''
    out = chatwoot.diagnostico()
    assert 'CHATWOOT_URL vazia' in out['conclusao']


def test_servidor_fora_conclui_hospedagem(app):
    import requests as req

    from app.services import chatwoot
    _cfg(app)
    with patch('app.services.chatwoot.requests.get',
               side_effect=req.ConnectionError('refused')):
        out = chatwoot.diagnostico()
    assert out['servidor_http'] is None
    assert 'hospedagem' in out['conclusao']
    assert 'Railway do Chatwoot' in out['conclusao']


def test_5xx_conclui_servidor_doente(app):
    from app.services import chatwoot
    _cfg(app)
    with patch('app.services.chatwoot.requests.get',
               return_value=_Resp(500, text='oops')):
        out = chatwoot.diagnostico()
    assert '5xx' in out['conclusao']
    assert 'Sidekiq' in out['conclusao']


def test_token_401_conclui_token_invalido(app):
    from app.services import chatwoot
    _cfg(app)

    def fake_get(url, **kw):
        if url.endswith('/api'):
            return _Resp(200, body={'version': '3.12.0'})
        return _Resp(401, body={'error': 'unauthorized'})

    with patch('app.services.chatwoot.requests.get', side_effect=fake_get):
        out = chatwoot.diagnostico()
    assert out['servidor_http'] == 200
    assert out['servidor_versao'] == '3.12.0'
    assert out['api_token_http'] == 401
    assert '401' in out['conclusao']
    assert 'token DESTE sistema' in out['conclusao']


def test_tudo_ok_conclui_canais_meta(app):
    """Servidor e tokens OK → o problema so pode estar nos canais Meta
    (tokens de WhatsApp/IG dentro do Chatwoot). E exatamente o quadro do
    incidente: IG '400 Session Invalid' com servidor de pe."""
    from app.services import chatwoot
    _cfg(app)

    def fake_get(url, **kw):
        if url.endswith('/api'):
            return _Resp(200, body={'version': '3.12.0'})
        return _Resp(200, body={'payload': []})

    with patch('app.services.chatwoot.requests.get', side_effect=fake_get):
        out = chatwoot.diagnostico()
    assert 'CANAIS (Meta)' in out['conclusao']
    assert 'Reauthorize' in out['conclusao']
    assert 'System User' in out['conclusao']


def test_diagnostico_nao_vaza_tokens(app):
    from app.services import chatwoot
    _cfg(app)

    def fake_get(url, **kw):
        return _Resp(200, body={'version': '3.12.0', 'payload': []})

    with patch('app.services.chatwoot.requests.get', side_effect=fake_get):
        out = chatwoot.diagnostico()
    blob = str(out)
    assert 'tok-api' not in blob
    assert 'tok-bot' not in blob


def test_rota_nega_admin_comum(app):
    """Admin nao-owner leva 403 (padrao owner_required — mesmo desenho
    dos outros /admin/debug-*)."""
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        comum = Usuario(nome='adm', login='adm_cw', papel='admin',
                        is_owner=False)
        comum.set_senha('senha123')
        db.session.add(comum)
        db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'adm_cw', 'senha': 'senha123'})
    assert c.get('/admin/debug-chatwoot').status_code == 403


def test_rota_responde_pro_owner(app):
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        dono = Usuario(nome='dono', login='dono_cw', papel='admin',
                       is_owner=True)
        dono.set_senha('senha123')
        db.session.add(dono)
        db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'dono_cw', 'senha': 'senha123'})
    app.config['CHATWOOT_URL'] = ''
    r = c.get('/admin/debug-chatwoot')
    assert r.status_code == 200
    assert 'conclusao' in r.get_json()

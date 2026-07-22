"""Cloudflare Stream — upload direto do navegador + player embutido.

A API do Cloudflare é SEMPRE mockada (padrão da casa, igual Anthropic/Seru):
nenhum teste toca a rede.
"""

import pytest

from app.extensions import db
from app.models import Treinamento, Usuario
from app.services import treinamento_stream as ts

UID = 'a' * 32
UPLOAD_URL = 'https://upload.videodelivery.net/deadbeef'


class FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 400
        self._payload = payload if payload is not None else {}

    def json(self):
        if self._payload is None:
            raise ValueError('sem json')
        return self._payload


def _config_stream(app):
    app.config['CLOUDFLARE_ACCOUNT_ID'] = 'acct123'
    app.config['CLOUDFLARE_STREAM_TOKEN'] = 'tok-secreto'
    app.config['CLOUDFLARE_STREAM_SUBDOMAIN'] = 'customer-abc'


def _admin(app, admin_user):
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    return c


# ── serviço ─────────────────────────────────────────────────────────────
def test_configurado_exige_as_duas_envs(app):
    with app.app_context():
        app.config['CLOUDFLARE_ACCOUNT_ID'] = ''
        app.config['CLOUDFLARE_STREAM_TOKEN'] = ''
        assert ts.configurado() is False
        app.config['CLOUDFLARE_ACCOUNT_ID'] = 'acct'
        assert ts.configurado() is False   # falta token
        app.config['CLOUDFLARE_STREAM_TOKEN'] = 'tok'
        assert ts.configurado() is True


def test_criar_upload_direto_ok(app, monkeypatch):
    with app.app_context():
        _config_stream(app)
        chamada = {}

        def fake_post(url, **kw):
            chamada['url'] = url
            chamada['headers'] = kw.get('headers')
            return FakeResp(200, {
                'success': True,
                'result': {'uid': UID, 'uploadURL': UPLOAD_URL}})
        monkeypatch.setattr(ts.requests, 'post', fake_post)
        r = ts.criar_upload_direto('aula 1')
        assert r == {'uid': UID, 'uploadURL': UPLOAD_URL}
        assert 'accounts/acct123/stream/direct_upload' in chamada['url']
        # o token vai no header, NUNCA na resposta pro navegador
        assert chamada['headers']['Authorization'] == 'Bearer tok-secreto'


def test_criar_upload_direto_sem_config_levanta(app):
    with app.app_context():
        app.config['CLOUDFLARE_ACCOUNT_ID'] = ''
        app.config['CLOUDFLARE_STREAM_TOKEN'] = ''
        with pytest.raises(ValueError, match='não configurado'):
            ts.criar_upload_direto('x')


def test_criar_upload_direto_api_recusa(app, monkeypatch):
    with app.app_context():
        _config_stream(app)
        monkeypatch.setattr(ts.requests, 'post', lambda url, **kw: FakeResp(
            403, {'success': False, 'errors': [{'message': 'sem permissão'}]}))
        with pytest.raises(ValueError, match='sem permissão'):
            ts.criar_upload_direto('x')


def test_status_pronto_e_pct(app, monkeypatch):
    with app.app_context():
        _config_stream(app)
        monkeypatch.setattr(ts.requests, 'get', lambda url, **kw: FakeResp(
            200, {'success': True, 'result': {
                'readyToStream': True,
                'status': {'state': 'ready', 'pctComplete': '100.0'}}}))
        st = ts.status(UID)
        assert st['pronto'] is True and st['pct'] == 100 and st['erro'] is None


def test_embed_url_usa_subdomain_do_config(app):
    with app.app_context():
        _config_stream(app)   # customer-abc -> vira host completo
        assert ts.embed_url(UID) == (
            f'https://customer-abc.cloudflarestream.com/{UID}/iframe')


def test_cachear_subdomain_pela_preview(app):
    with app.app_context():
        app.config['CLOUDFLARE_ACCOUNT_ID'] = 'a'
        app.config['CLOUDFLARE_STREAM_TOKEN'] = 't'
        app.config['CLOUDFLARE_STREAM_SUBDOMAIN'] = ''   # força descobrir
        from app.models.config import AppConfig
        ts._cachear_subdomain({
            'preview': f'https://customer-xyz.cloudflarestream.com/{UID}/watch'})
        assert AppConfig.get('cloudflare_stream_subdomain') == \
            'customer-xyz.cloudflarestream.com'
        assert ts.embed_url(UID) == \
            f'https://customer-xyz.cloudflarestream.com/{UID}/iframe'


# ── rotas ───────────────────────────────────────────────────────────────
def test_upload_url_503_sem_config(app, admin_user):
    app.config['CLOUDFLARE_ACCOUNT_ID'] = ''
    app.config['CLOUDFLARE_STREAM_TOKEN'] = ''
    with app.app_context():
        t = Treinamento(titulo='S1')
        db.session.add(t)
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    r = c.post(f'/treinamento/admin/{tid}/video/stream/upload-url')
    assert r.status_code == 503


def test_upload_url_devolve_uid_e_url(app, admin_user, monkeypatch):
    _config_stream(app)
    monkeypatch.setattr(ts.requests, 'post', lambda url, **kw: FakeResp(
        200, {'success': True,
              'result': {'uid': UID, 'uploadURL': UPLOAD_URL}}))
    with app.app_context():
        t = Treinamento(titulo='S2')
        db.session.add(t)
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    r = c.post(f'/treinamento/admin/{tid}/video/stream/upload-url',
               data={'nome': 'aula.mp4'})
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] and j['uid'] == UID and j['uploadURL'] == UPLOAD_URL


def test_salvar_grava_uid_e_troca_tipo(app, admin_user):
    _config_stream(app)
    with app.app_context():
        t = Treinamento(titulo='S3')
        db.session.add(t)
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    r = c.post(f'/treinamento/admin/{tid}/video/stream/salvar',
               data={'uid': UID})
    assert r.status_code == 200 and r.get_json()['ok']
    with app.app_context():
        t = db.session.get(Treinamento, tid)
        assert t.video_tipo == 'stream' and t.video_ref == UID


def test_salvar_recusa_uid_invalido(app, admin_user):
    _config_stream(app)
    with app.app_context():
        t = Treinamento(titulo='S4')
        db.session.add(t)
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    r = c.post(f'/treinamento/admin/{tid}/video/stream/salvar',
               data={'uid': '../etc/passwd'})
    assert r.status_code == 400
    with app.app_context():
        assert db.session.get(Treinamento, tid).video_ref is None


def test_troca_de_stream_apaga_o_anterior_no_cloudflare(app, admin_user,
                                                        monkeypatch):
    _config_stream(app)
    apagados = []
    monkeypatch.setattr(ts, 'deletar', lambda uid: apagados.append(uid))
    velho = 'b' * 32
    with app.app_context():
        t = Treinamento(titulo='S5', video_tipo='stream', video_ref=velho)
        db.session.add(t)
        db.session.commit()
        tid = t.id
    c = _admin(app, admin_user)
    c.post(f'/treinamento/admin/{tid}/video/stream/salvar', data={'uid': UID})
    assert apagados == [velho]   # o vídeo antigo do Cloudflare foi removido


def test_aluno_ve_iframe_do_stream(app, tmp_path):
    """Funcionário vê o player embutido (iframe) — nunca sai do site."""
    _config_stream(app)
    with app.app_context():
        t = Treinamento(titulo='S6', ativo=True, video_tipo='stream',
                        video_ref=UID)
        db.session.add(t)
        db.session.commit()
        tid = t.id
        u = Usuario(nome='F', login='f-stream', papel='funcionario')
        u.set_senha('x' * 8)
        db.session.add(u)
        db.session.commit()
        uid_user = u.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(uid_user)
        s['_fresh'] = True
    body = c.get(f'/treinamento/{tid}/assistir').get_data(as_text=True)
    assert f'customer-abc.cloudflarestream.com/{UID}/iframe' in body
    assert 'embed.cloudflarestream.com/embed/sdk' in body   # SDK do auto-marcar

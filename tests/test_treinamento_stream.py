"""Cloudflare Stream — upload direto do navegador + player embutido.

`treinamento_stream` é o serviço COMPARTILHADO de vídeo (Cloudflare); sobreviveu
à remoção do módulo antigo de vídeos (24/07/2026). As rotas exercitadas aqui são
as do módulo NOVO gamificado (`treino`). A API do Cloudflare é SEMPRE mockada
(padrão da casa, igual Anthropic/Seru): nenhum teste toca a rede.
"""

import pytest

from app.extensions import db
from app.models import TreinoTrilha, TreinoVideo, Usuario
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


def _video(app, **kw):
    """Cria uma trilha + vídeo no módulo novo e devolve o id do vídeo."""
    with app.app_context():
        t = TreinoTrilha(nome='Seg')
        db.session.add(t)
        db.session.commit()
        v = TreinoVideo(trilha_id=t.id, titulo=kw.pop('titulo', 'Aula'),
                        duracao_segundos=kw.pop('duracao', 60), **kw)
        db.session.add(v)
        db.session.commit()
        return v.id


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


# ── rotas (módulo NOVO gamificado) ──────────────────────────────────────
def test_upload_url_503_sem_config(app, admin_user):
    app.config['CLOUDFLARE_ACCOUNT_ID'] = ''
    app.config['CLOUDFLARE_STREAM_TOKEN'] = ''
    vid = _video(app)
    c = _admin(app, admin_user)
    r = c.post(f'/treino/admin/video/{vid}/upload-url')
    assert r.status_code == 503


def test_upload_url_devolve_uid_e_url(app, admin_user, monkeypatch):
    _config_stream(app)
    monkeypatch.setattr(ts.requests, 'post', lambda url, **kw: FakeResp(
        200, {'success': True,
              'result': {'uid': UID, 'uploadURL': UPLOAD_URL}}))
    vid = _video(app)
    c = _admin(app, admin_user)
    r = c.post(f'/treino/admin/video/{vid}/upload-url')
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] and j['uid'] == UID and j['uploadURL'] == UPLOAD_URL


def test_upload_url_502_se_api_recusa(app, admin_user, monkeypatch):
    _config_stream(app)
    monkeypatch.setattr(ts.requests, 'post', lambda url, **kw: FakeResp(
        403, {'success': False, 'errors': [{'message': 'sem permissão'}]}))
    vid = _video(app)
    c = _admin(app, admin_user)
    r = c.post(f'/treino/admin/video/{vid}/upload-url')
    assert r.status_code == 502 and not r.get_json()['ok']


def test_salvar_grava_uid_e_provedor(app, admin_user):
    _config_stream(app)
    vid = _video(app, titulo='S3')
    c = _admin(app, admin_user)
    r = c.post(f'/treino/admin/video/{vid}/salvar', data={'uid': UID})
    assert r.status_code == 200 and r.get_json()['ok']
    with app.app_context():
        v = db.session.get(TreinoVideo, vid)
        assert v.provedor == 'cloudflare' and v.video_externo_id == UID
        assert v.ativo is False and v.duracao_segundos == 0


def test_upload_browser_nao_salva_resposta_recusada(app, admin_user):
    """XHR ``load`` também dispara em HTTP 4xx; a tela precisa conferir o
    status antes de gravar o UID e avisar o limite do POST simples."""
    _config_stream(app)
    vid = _video(app, titulo='Upload')
    body = _admin(app, admin_user).get(
        f'/treino/admin/video/{vid}').get_data(as_text=True)
    assert 'xhr.status<200||xhr.status>=300' in body
    assert '200*1024*1024' in body
    assert 'O Cloudflare recusou o upload' in body


def test_salvar_recusa_uid_invalido(app, admin_user):
    _config_stream(app)
    vid = _video(app, titulo='S4')
    c = _admin(app, admin_user)
    r = c.post(f'/treino/admin/video/{vid}/salvar',
               data={'uid': '../etc/passwd'})
    assert r.status_code == 400
    with app.app_context():
        assert db.session.get(TreinoVideo, vid).video_externo_id is None


def test_salvar_descobre_subdomain_sem_env(app, admin_user, monkeypatch):
    """Sem a env CLOUDFLARE_STREAM_SUBDOMAIN, o salvar consulta o vídeo e
    DESCOBRE o subdomínio pela URL de preview — senão o player não montaria."""
    app.config['CLOUDFLARE_ACCOUNT_ID'] = 'acct'
    app.config['CLOUDFLARE_STREAM_TOKEN'] = 'tok'
    app.config['CLOUDFLARE_STREAM_SUBDOMAIN'] = ''      # SEM a env opcional
    monkeypatch.setattr(ts.requests, 'get', lambda url, **kw: FakeResp(200, {
        'success': True, 'result': {
            'readyToStream': False,
            'preview': f'https://customer-zzz.cloudflarestream.com/{UID}/watch',
            'status': {'pctComplete': '10'}}}))
    vid = _video(app, titulo='Sub')
    c = _admin(app, admin_user)
    r = c.post(f'/treino/admin/video/{vid}/salvar', data={'uid': UID})
    assert r.status_code == 200
    with app.app_context():
        from app.models.config import AppConfig
        assert AppConfig.get('cloudflare_stream_subdomain') == \
            'customer-zzz.cloudflarestream.com'
        assert ts.embed_url(UID) == \
            f'https://customer-zzz.cloudflarestream.com/{UID}/iframe'


def test_excluir_video_apaga_no_cloudflare(app, admin_user, monkeypatch):
    """Excluir um vídeo do Stream NÃO pode deixar o vídeo órfão no Cloudflare."""
    _config_stream(app)
    apagados = []
    monkeypatch.setattr(ts, 'deletar', lambda uid: apagados.append(uid))
    vid = _video(app, titulo='Del', video_externo_id=UID, provedor='cloudflare')
    c = _admin(app, admin_user)
    c.post(f'/treino/admin/video/{vid}/excluir')
    assert apagados == [UID]   # o vídeo do Cloudflare foi removido junto


def test_aluno_ve_iframe_do_stream(app):
    """Funcionário vê o player embutido (iframe) — nunca sai do site."""
    _config_stream(app)
    vid = _video(app, titulo='S6', ativo=True, video_externo_id=UID,
                 provedor='cloudflare')
    with app.app_context():
        u = Usuario(nome='F', login='f-stream', papel='funcionario')
        u.set_senha('x' * 8)
        db.session.add(u)
        db.session.commit()
        uid_user = u.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(uid_user)
        s['_fresh'] = True
    body = c.get(f'/treino/video/{vid}').get_data(as_text=True)
    assert f'customer-abc.cloudflarestream.com/{UID}/iframe' in body
    assert 'embed.cloudflarestream.com/embed/sdk' in body   # SDK do auto-marcar

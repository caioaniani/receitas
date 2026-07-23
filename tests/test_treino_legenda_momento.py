"""IA sugere o MOMENTO do checkpoint a partir da legenda automática do
Cloudflare. Cloudflare e Anthropic sempre mockados (padrão da casa).
"""
import json as _json
from unittest.mock import MagicMock, patch

from app.extensions import db
from app.models import TreinoTrilha, TreinoVideo
from app.services import treinamento_stream as ts
from app.services import treino_ia_perguntas as ia

VTT = """WEBVTT

00:00:00.000 --> 00:00:03.000
Bom dia, hoje vamos falar de higiene.

00:02:30.500 --> 00:02:34.000
Lave as mãos antes de manipular alimentos.
"""


def _admin(app, admin_user):
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    return c


def _video(app, **kw):
    with app.app_context():
        t = TreinoTrilha(nome='Seg')
        db.session.add(t)
        db.session.commit()
        v = TreinoVideo(trilha_id=t.id, titulo='Aula', **kw)
        db.session.add(v)
        db.session.commit()
        return v.id


def _cfg(app):
    app.config['CLOUDFLARE_ACCOUNT_ID'] = 'acct'
    app.config['CLOUDFLARE_STREAM_TOKEN'] = 'tok'


# ── parser VTT ──────────────────────────────────────────────────────────
def test_parse_vtt_extrai_tempo_e_texto():
    segs = ts._parse_vtt(VTT)
    assert len(segs) == 2
    assert segs[0]['inicio'] == 0 and 'higiene' in segs[0]['texto'].lower()
    assert segs[1]['inicio'] == 150   # 00:02:30 -> 150s
    assert 'mãos' in segs[1]['texto'].lower()


def test_vtt_ts_para_seg():
    assert ts._vtt_ts_para_seg('00:02:30.500') == 150
    assert ts._vtt_ts_para_seg('02:30.000') == 150   # MM:SS
    assert ts._vtt_ts_para_seg('01:00:00') == 3600
    assert ts._vtt_ts_para_seg('lixo') == 0


# ── serviço: transcricao / gerar_legenda ────────────────────────────────
def test_transcricao_baixa_e_parseia_vtt(app, monkeypatch):
    with app.app_context():
        _cfg(app)
        resp = MagicMock(); resp.status_code = 200; resp.text = VTT
        monkeypatch.setattr(ts.requests, 'get', lambda *a, **k: resp)
        segs = ts.transcricao('a' * 32)
        assert len(segs) == 2 and segs[1]['inicio'] == 150


def test_transcricao_vazia_quando_nao_pronta(app, monkeypatch):
    with app.app_context():
        _cfg(app)
        resp = MagicMock(); resp.status_code = 404; resp.text = ''
        monkeypatch.setattr(ts.requests, 'get', lambda *a, **k: resp)
        assert ts.transcricao('a' * 32) == []


def test_gerar_legenda_dispara(app, monkeypatch):
    with app.app_context():
        _cfg(app)
        chamou = {}
        resp = MagicMock(); resp.ok = True
        resp.json = lambda: {'success': True, 'result': {'status': 'inprogress'}}

        def fake_post(url, **k):
            chamou['url'] = url
            return resp
        monkeypatch.setattr(ts.requests, 'post', fake_post)
        r = ts.gerar_legenda('a' * 32)
        assert r['ok'] and 'captions/pt/generate' in chamou['url']


# ── IA com momento ──────────────────────────────────────────────────────
def _fake_ia(payload):
    blk = MagicMock(); blk.type = 'text'; blk.text = _json.dumps(payload)
    resp = MagicMock(); resp.content = [blk]; resp.usage = None
    client = MagicMock(); client.messages.create.return_value = resp
    return client


def test_gerar_com_momento_sanitiza(app, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    payload = [
        {'enunciado': 'Quando lavar a mão?', 'alternativas': ['Antes', 'Nunca'],
         'correta': 0, 'momento_seg': 150, 'dificuldade': 'FACIL'},
        {'enunciado': 'Sem momento', 'alternativas': ['a', 'b'], 'correta': 0},
        {'enunciado': 'Momento alucinado', 'alternativas': ['a', 'b'],
         'correta': 1, 'momento_seg': 99999},   # além do vídeo -> capado
    ]
    segs = [{'inicio': 150, 'texto': 'lave as mãos'}]
    with app.app_context(), \
            patch('anthropic.Anthropic', return_value=_fake_ia(payload)):
        r = ia.gerar_com_momento(segs, n=3)
    assert 'perguntas' in r and len(r['perguntas']) == 3
    assert r['perguntas'][0]['momento_seg'] == 150
    assert r['perguntas'][1]['momento_seg'] is None   # ausente -> sem sugestão
    assert r['perguntas'][2]['momento_seg'] == 150     # capado no maior tempo


def test_gerar_com_momento_sem_transcricao():
    r = ia.gerar_com_momento([])
    assert 'erro' in r


# ── rota fonte=video ────────────────────────────────────────────────────
def test_rota_video_processando_quando_sem_legenda(app, admin_user, monkeypatch):
    _cfg(app)
    vid = _video(app, video_externo_id='a' * 32, provedor='cloudflare')
    monkeypatch.setattr(ts, 'transcricao', lambda uid: [])
    disparou = {}

    def fake_gerar(uid):
        disparou['x'] = True
        return {'ok': True, 'status': 'inprogress', 'erro': None}
    monkeypatch.setattr(ts, 'gerar_legenda', fake_gerar)
    c = _admin(app, admin_user)
    r = c.post(f'/treino/admin/video/{vid}/ia-gerar', data={'fonte': 'video'})
    assert r.status_code == 409
    j = r.get_json()
    assert j['processando'] and disparou.get('x')   # disparou a geração


def test_rota_video_surfacea_erro_da_legenda(app, admin_user, monkeypatch):
    _cfg(app)
    vid = _video(app, video_externo_id='a' * 32, provedor='cloudflare')
    monkeypatch.setattr(ts, 'transcricao', lambda uid: [])
    monkeypatch.setattr(ts, 'gerar_legenda', lambda uid: {
        'ok': False, 'status': None, 'erro': 'video ainda não transcodificado'})
    c = _admin(app, admin_user)
    r = c.post(f'/treino/admin/video/{vid}/ia-gerar', data={'fonte': 'video'})
    assert r.status_code == 409
    # mostra o motivo REAL, não o genérico "processando"
    assert 'transcodificado' in r.get_json()['erro']


def test_rota_video_com_legenda_devolve_momento(app, admin_user, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    _cfg(app)
    vid = _video(app, video_externo_id='a' * 32, provedor='cloudflare')
    monkeypatch.setattr(ts, 'transcricao',
                        lambda uid: [{'inicio': 150, 'texto': 'lave as mãos'}])
    payload = [{'enunciado': 'Q?', 'alternativas': ['a', 'b'], 'correta': 1,
                'momento_seg': 150, 'dificuldade': 'MEDIA'}]
    c = _admin(app, admin_user)
    with patch('anthropic.Anthropic', return_value=_fake_ia(payload)):
        r = c.post(f'/treino/admin/video/{vid}/ia-gerar',
                   data={'fonte': 'video'})
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] and j['perguntas'][0]['momento_seg'] == 150


def test_rota_video_sem_upload_recusa(app, admin_user):
    vid = _video(app)   # sem video_externo_id
    c = _admin(app, admin_user)
    r = c.post(f'/treino/admin/video/{vid}/ia-gerar', data={'fonte': 'video'})
    assert r.status_code == 400

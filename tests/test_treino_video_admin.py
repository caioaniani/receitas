"""Admin do vídeo (treino): momento em min:seg, duração auto do Cloudflare,
geração de pergunta por IA no checkpoint. Cloudflare/Anthropic sempre mockados.
"""
import json as _json
from unittest.mock import MagicMock, patch

from app.blueprints.treino.routes import _mmss_para_seg
from app.extensions import db
from app.models import TreinoCheckpoint, TreinoTrilha, TreinoVideo


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


def test_mmss_para_seg():
    assert _mmss_para_seg('2:30') == 150
    assert _mmss_para_seg('0:45') == 45
    assert _mmss_para_seg('10:00') == 600
    assert _mmss_para_seg('150') == 150      # segundos crus ainda valem
    assert _mmss_para_seg('') == 0
    assert _mmss_para_seg('lixo') == 0
    assert _mmss_para_seg('1:') == 60
    assert _mmss_para_seg('1:02:30') == 3750   # hora:min:seg
    assert _mmss_para_seg('-5') == 0           # nunca negativo
    assert _mmss_para_seg('90:00') == 5400     # minutos > 60 valem


def test_checkpoint_aceita_mmss(app, admin_user):
    vid = _video(app)
    c = _admin(app, admin_user)
    c.post(f'/treino/admin/video/{vid}/checkpoint', data={
        'segundo': '2:30', 'enunciado': 'Quando lavar a mão?',
        'alt[]': ['Antes', 'Nunca'], 'correta': '0'})
    with app.app_context():
        cp = TreinoCheckpoint.query.filter_by(video_id=vid).first()
        assert cp is not None and cp.segundo == 150


def test_duracao_detectada_do_cloudflare_no_load(app, admin_user):
    vid = _video(app, video_externo_id='a' * 32, provedor='cloudflare',
                 duracao_segundos=0)
    c = _admin(app, admin_user)
    with patch('app.services.treinamento_stream.status',
               return_value={'pronto': True, 'pct': 100, 'duracao': 213,
                             'erro': None}):
        r = c.get(f'/treino/admin/video/{vid}')
    assert r.status_code == 200
    with app.app_context():
        assert db.session.get(TreinoVideo, vid).duracao_segundos == 213


def _fake_ia(payload):
    blk = MagicMock(); blk.type = 'text'; blk.text = _json.dumps(payload)
    resp = MagicMock(); resp.content = [blk]; resp.usage = None
    client = MagicMock(); client.messages.create.return_value = resp
    return client


def test_checkpoint_ia_gera_proposta(app, admin_user, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-teste')
    vid = _video(app)
    payload = [{'enunciado': 'Qual a temperatura?', 'dificuldade': 'FACIL',
                'alternativas': ['5C', '60C'], 'correta': 1}]
    c = _admin(app, admin_user)
    conteudo = 'Conservação a frio exige temperatura controlada. ' * 4
    with patch('anthropic.Anthropic', return_value=_fake_ia(payload)):
        r = c.post(f'/treino/admin/video/{vid}/ia-gerar',
                   data={'texto': conteudo, 'n': 3})
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] and j['perguntas'][0]['correta'] == 1

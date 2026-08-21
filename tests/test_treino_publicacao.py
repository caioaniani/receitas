"""Publicação independente de módulo e aula no treinamento."""
from unittest.mock import patch

from app.extensions import db
from app.models import AppConfig, TreinoTrilha, TreinoVideo
from app.services import treino_trilha as tt


def _admin(app, admin_user):
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    return c


def test_modulo_mostra_somente_aula_publicada_com_video(app, admin_user):
    with app.app_context():
        t = TreinoTrilha(nome='Módulo 1', ativa=True)
        db.session.add(t)
        db.session.flush()
        db.session.add_all([
            TreinoVideo(trilha_id=t.id, titulo='Aula 1.1', ativo=True,
                        video_externo_id='1' * 32, ordem=1),
            TreinoVideo(trilha_id=t.id, titulo='Aula 1.2', ativo=True,
                        video_externo_id=None, ordem=2),
            TreinoVideo(trilha_id=t.id, titulo='Aula 1.3', ativo=False,
                        video_externo_id='3' * 32, ordem=3),
        ])
        db.session.commit()
        tid = t.id

    body = _admin(app, admin_user).get(
        f'/treino/trilha/{tid}').get_data(as_text=True)
    assert 'Aula 1.1' in body
    assert 'Aula 1.2' not in body
    assert 'Aula 1.3' not in body


def test_progresso_ignora_titulo_ativo_sem_video(app):
    with app.app_context():
        t = TreinoTrilha(nome='Módulo 1', ativa=True)
        db.session.add(t)
        db.session.flush()
        pronta = TreinoVideo(
            trilha_id=t.id, titulo='Aula 1.1', ativo=True,
            video_externo_id='1' * 32, ordem=1)
        vazia = TreinoVideo(
            trilha_id=t.id, titulo='Aula 1.2', ativo=True, ordem=2)
        db.session.add_all([pronta, vazia])
        db.session.commit()
        assert tt.videos_publicados(t) == [pronta]


def test_nova_aula_e_novo_modulo_nascem_como_rascunho(app, admin_user):
    c = _admin(app, admin_user)
    c.post('/treino/admin/trilha', data={'nome': 'Módulo novo'})
    with app.app_context():
        t = TreinoTrilha.query.filter_by(nome='Módulo novo').one()
        assert t.ativa is False
        tid = t.id
    c.post(f'/treino/admin/trilha/{tid}/video', data={'titulo': 'Aula 1.1'})
    with app.app_context():
        assert TreinoVideo.query.filter_by(
            trilha_id=tid, titulo='Aula 1.1').one().ativo is False


def test_nao_publica_aula_sem_video(app, admin_user):
    with app.app_context():
        t = TreinoTrilha(nome='Módulo 1', ativa=False)
        db.session.add(t)
        db.session.flush()
        v = TreinoVideo(trilha_id=t.id, titulo='Aula 1.1', ativo=False)
        db.session.add(v)
        db.session.commit()
        vid = v.id
    r = _admin(app, admin_user).post(
        f'/treino/admin/video/{vid}/toggle', data={'acao': 'publicar'},
        follow_redirects=True)
    assert 'Envie o arquivo do vídeo antes de publicar' in r.get_data(
        as_text=True)
    with app.app_context():
        assert db.session.get(TreinoVideo, vid).ativo is False


def test_publica_aula_somente_quando_cloudflare_pronto(app, admin_user):
    with app.app_context():
        t = TreinoTrilha(nome='Módulo 1', ativa=False)
        db.session.add(t)
        db.session.flush()
        v = TreinoVideo(
            trilha_id=t.id, titulo='Aula 1.1', ativo=False,
            video_externo_id='1' * 32)
        db.session.add(v)
        db.session.commit()
        vid = v.id
    c = _admin(app, admin_user)
    with patch('app.services.treinamento_stream.status', return_value={
            'pronto': False, 'pct': 30, 'erro': None}):
        c.post(f'/treino/admin/video/{vid}/toggle',
               data={'acao': 'publicar'})
    with app.app_context():
        assert db.session.get(TreinoVideo, vid).ativo is False
    with patch('app.services.treinamento_stream.status', return_value={
            'pronto': True, 'pct': 100, 'erro': None, 'duracao': 213}):
        c.post(f'/treino/admin/video/{vid}/toggle',
               data={'acao': 'publicar'})
    with app.app_context():
        v = db.session.get(TreinoVideo, vid)
        assert v.ativo is True and v.duracao_segundos == 213


def test_backfill_move_so_aulas_sem_video_para_rascunho(app):
    from app.migrations_legacy import (
        _backfill_treino_aulas_sem_video_rascunho,
    )

    with app.app_context():
        t = TreinoTrilha(nome='Módulo 1', ativa=True)
        db.session.add(t)
        db.session.flush()
        com_video = TreinoVideo(
            trilha_id=t.id, titulo='Aula 1.1', ativo=True,
            video_externo_id='1' * 32)
        sem_video = TreinoVideo(
            trilha_id=t.id, titulo='Aula 1.2', ativo=True)
        db.session.add_all([com_video, sem_video])
        db.session.commit()
        _backfill_treino_aulas_sem_video_rascunho(app)
        db.session.refresh(com_video)
        db.session.refresh(sem_video)
        assert com_video.ativo is True
        assert sem_video.ativo is False
        assert AppConfig.get(
            'treino_aulas_sem_video_rascunho_2026_08_21') == 'desativadas=1'

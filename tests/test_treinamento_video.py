"""Serviço de vídeo do treinamento (self-host): salvar em blocos + servir com
HTTP Range. API Anthropic/Dropbox não entram aqui — é só disco local."""
import os
from io import BytesIO

import pytest
from werkzeug.datastructures import FileStorage

from app.services import treinamento_video as tv


@pytest.fixture
def media(app, tmp_path):
    """Aponta a pasta de mídia pra um tmp isolado por teste."""
    with app.app_context():
        app.config['TREINAMENTO_MEDIA_DIR'] = str(tmp_path)
        yield tmp_path


def _fs(dados, nome='aula.mp4'):
    return FileStorage(stream=BytesIO(dados), filename=nome)


def test_salvar_grava_no_volume_e_retorna_ref(app, media):
    with app.app_context():
        ref = tv.salvar_video(_fs(b'ABCDEFGHIJ'), treino_id=7)
        assert ref.startswith('treino-7-') and ref.endswith('.mp4')
        caminho = os.path.join(str(media), ref)
        assert os.path.exists(caminho)
        with open(caminho, 'rb') as f:
            assert f.read() == b'ABCDEFGHIJ'


def test_salvar_rejeita_extensao_nao_video(app, media):
    with app.app_context():
        with pytest.raises(ValueError):
            tv.salvar_video(_fs(b'x', nome='virus.exe'), treino_id=1)


def test_caminho_bloqueia_path_traversal(app, media):
    with app.app_context():
        assert tv.caminho_video('../../etc/passwd') is None
        assert tv.caminho_video('sub/dir.mp4') is None
        assert tv.caminho_video('treino-1-abc.mp4') is not None


def test_servir_inteiro_200(app, media):
    with app.app_context():
        ref = tv.salvar_video(_fs(b'0123456789'), treino_id=1)
    with app.test_request_context('/'):
        r = tv.resposta_range(ref)
        assert r.status_code == 200
        assert r.headers['Accept-Ranges'] == 'bytes'
        assert r.headers['Content-Length'] == '10'
        assert b''.join(r.response) == b'0123456789'


def test_servir_range_206_com_content_range(app, media):
    with app.app_context():
        ref = tv.salvar_video(_fs(b'0123456789'), treino_id=1)
    with app.test_request_context('/', headers={'Range': 'bytes=2-5'}):
        r = tv.resposta_range(ref)
        assert r.status_code == 206
        assert r.headers['Content-Range'] == 'bytes 2-5/10'
        assert r.headers['Content-Length'] == '4'
        assert b''.join(r.response) == b'2345'


def test_servir_range_aberto_ate_o_fim(app, media):
    with app.app_context():
        ref = tv.salvar_video(_fs(b'0123456789'), treino_id=1)
    with app.test_request_context('/', headers={'Range': 'bytes=7-'}):
        r = tv.resposta_range(ref)
        assert r.status_code == 206
        assert r.headers['Content-Range'] == 'bytes 7-9/10'
        assert b''.join(r.response) == b'789'


def test_servir_range_invalido_416(app, media):
    with app.app_context():
        ref = tv.salvar_video(_fs(b'0123456789'), treino_id=1)
    with app.test_request_context('/', headers={'Range': 'bytes=50-60'}):
        r = tv.resposta_range(ref)
        assert r.status_code == 416
        assert r.headers['Content-Range'] == 'bytes */10'


def test_servir_arquivo_inexistente_404(app, media):
    with app.app_context(), app.test_request_context('/'):
        assert tv.resposta_range('treino-9-naoexiste.mp4').status_code == 404


def test_remover_apaga_o_arquivo(app, media):
    with app.app_context():
        ref = tv.salvar_video(_fs(b'xyz'), treino_id=1)
        assert os.path.exists(os.path.join(str(media), ref))
        tv.remover_video(ref)
        assert not os.path.exists(os.path.join(str(media), ref))

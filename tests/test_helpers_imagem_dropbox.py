"""Testes dos helpers de compressao e upload generico do Dropbox."""
import io
from unittest.mock import patch

import pytest


def _fake_image_bytes(size=(1000, 1000), mode='RGB', fmt='JPEG'):
    """Gera bytes de uma imagem real via PIL pra teste."""
    from PIL import Image
    img = Image.new(mode, size, color='red')
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def test_comprimir_imagem_reduz_dimensoes(app):
    from app.utils import comprimir_imagem
    raw = _fake_image_bytes(size=(2000, 1500))
    out = comprimir_imagem(raw)

    from PIL import Image
    img = Image.open(io.BytesIO(out))
    assert max(img.size) == 700, f'esperava max 700px, veio {img.size}'
    assert img.format == 'JPEG'


def test_comprimir_imagem_preserva_imagem_pequena_em_jpeg(app):
    from app.utils import comprimir_imagem
    raw = _fake_image_bytes(size=(300, 200))
    out = comprimir_imagem(raw)
    from PIL import Image
    img = Image.open(io.BytesIO(out))
    assert img.size == (300, 200), 'thumbnail nao aumenta'
    assert img.format == 'JPEG'


def test_comprimir_imagem_converte_rgba_pra_rgb(app):
    from app.utils import comprimir_imagem
    raw = _fake_image_bytes(size=(500, 500), mode='RGBA', fmt='PNG')
    out = comprimir_imagem(raw)
    from PIL import Image
    img = Image.open(io.BytesIO(out))
    assert img.mode == 'RGB', 'RGBA deve virar RGB no JPEG'


def test_comprimir_imagem_vazio_levanta(app):
    from app.utils import comprimir_imagem
    with pytest.raises(ValueError, match='vazio'):
        comprimir_imagem(b'')


def test_comprimir_imagem_max_size_customizado(app):
    from app.utils import comprimir_imagem
    raw = _fake_image_bytes(size=(2000, 2000))
    out = comprimir_imagem(raw, max_size=300, quality=70)
    from PIL import Image
    img = Image.open(io.BytesIO(out))
    assert max(img.size) == 300


def test_upload_publico_sobe_e_retorna_url(app):
    """upload_publico chama API Dropbox e retorna URL publica."""
    from app.services import dropbox_storage

    chamadas = []

    def fake_post(url, **kwargs):
        chamadas.append((url, kwargs))

        class R:
            status_code = 200

            def json(self):
                if 'sharing/create_shared_link' in url:
                    return {'url': 'https://www.dropbox.com/s/abc/foo.jpg?dl=0'}
                return {'path_lower': '/test/foo.jpg', 'size': 100}
        return R()

    with app.app_context(), \
         patch('app.services.dropbox_storage._token', return_value='fake_token'), \
         patch('app.services.dropbox_storage.requests.post', side_effect=fake_post):
        r = dropbox_storage.upload_publico(b'fakebytes' * 10, '/test/foo.jpg')

    assert r['storage_path'] == '/test/foo.jpg'
    assert r['url'] == 'https://www.dropbox.com/s/abc/foo.jpg?raw=1'  # ?dl=0 → ?raw=1
    assert r['tamanho'] == 100
    # Conferir que chamou /upload e /create_shared_link
    urls_chamadas = [c[0] for c in chamadas]
    assert any('/files/upload' in u for u in urls_chamadas)
    assert any('/sharing/create_shared_link' in u for u in urls_chamadas)


def test_upload_publico_path_invalido(app):
    from app.services import dropbox_storage
    with app.app_context():
        with pytest.raises(RuntimeError, match='deve comecar com /'):
            dropbox_storage.upload_publico(b'x', 'sem-barra-inicial.jpg')


def test_upload_publico_arquivo_vazio(app):
    from app.services import dropbox_storage
    with app.app_context():
        with pytest.raises(RuntimeError, match='vazio'):
            dropbox_storage.upload_publico(b'', '/foo.jpg')


def test_converter_para_raw_remove_dl_e_normaliza():
    """URL Dropbox com dl=0 e/ou raw=1 vira sempre `&raw=1` unico, sem dl."""
    from app.services.dropbox_storage import _converter_para_raw

    casos = [
        # (input, esperado-parcial-no-output)
        ('https://www.dropbox.com/s/abc/foo.jpg?dl=0', 'raw=1'),
        ('https://www.dropbox.com/scl/fi/x/foo.jpg?rlkey=k&dl=0', 'raw=1'),
        ('https://www.dropbox.com/scl/fi/x/foo.jpg?rlkey=k&dl=0&raw=1', 'raw=1'),
        ('https://www.dropbox.com/scl/fi/x/foo.jpg?rlkey=k&raw=1&raw=1', 'raw=1'),
        ('https://www.dropbox.com/s/abc/foo.jpg', 'raw=1'),  # sem dl, adiciona
    ]
    for entrada, esperado in casos:
        out = _converter_para_raw(entrada)
        assert 'dl=' not in out, f'dl ainda em {out}'
        assert out.count('raw=1') == 1, f'raw=1 duplicado em {out}'
        assert esperado in out


def test_upload_foto_delega_pra_upload_publico(app):
    """upload_foto (compat) deve chamar upload_publico com path correto."""
    from app.services import dropbox_storage

    chamadas = []

    def fake_publico(file_bytes, path, **kwargs):
        chamadas.append({'bytes': file_bytes, 'path': path, 'kwargs': kwargs})
        return {'url': 'https://x', 'storage_path': path, 'tamanho': len(file_bytes)}

    with app.app_context(), \
         patch('app.services.dropbox_storage.upload_publico', side_effect=fake_publico):
        r = dropbox_storage.upload_foto(b'fakefile', atribuicao_id=42, ext='jpg')

    assert len(chamadas) == 1
    path = chamadas[0]['path']
    assert '/42_' in path, 'path deve conter atribuicao_id'
    assert path.endswith('.jpg')
    assert r['url'] == 'https://x'

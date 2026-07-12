"""Regressao: fotos do Dropbox no PDF de relatorio de pedidos.

Bug (24/06/2026): foto aparecia na tela mas sumia do PDF.
Fix em 2 etapas:
1. User-Agent + raw=1 (NAO BASTOU em prod).
2. Baixar via API autenticada (`dropbox_storage.baixar(storage_path)`),
   tirando o CDN publico do caminho critico.

Os testes garantem a ordem: API autenticada PRIMEIRO; shared link como
fallback; BLOB legado por ultimo.
"""
import io
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from app.services import relatorio


def _jpeg_bytes():
    img = Image.new('RGB', (120, 90), (180, 90, 40))
    buf = io.BytesIO()
    img.save(buf, format='JPEG')
    return buf.getvalue()


def _resp(status, content, content_type):
    return SimpleNamespace(
        status_code=status, content=content,
        headers={'Content-Type': content_type})


def test_eh_imagem_aceita_jpeg_png_por_magic():
    assert relatorio._eh_imagem(_jpeg_bytes(), '')
    assert relatorio._eh_imagem(b'\x89PNG\r\n\x1a\n....', '')
    # HTML nao e imagem
    assert not relatorio._eh_imagem(b'<!DOCTYPE html><html>...', 'text/html')


def test_eh_imagem_aceita_por_content_type():
    assert relatorio._eh_imagem(b'qualquercoisa', 'image/jpeg')


def test_foto_bytes_usa_api_autenticada_primeiro():
    """Caminho canonico: storage_path + API autenticada do Dropbox.
    Nem chega a tocar no shared link."""
    jpeg = _jpeg_bytes()
    foto = SimpleNamespace(id=1, imagem_url='https://dropbox.com/x?raw=1',
                           imagem_storage_path='/recebimento/1/abc.jpg',
                           imagem=None)
    with patch('app.services.dropbox_storage.baixar', return_value=jpeg) as api, \
         patch('app.services.relatorio.requests.get') as http:
        out = relatorio._foto_bytes(foto)
    assert out == jpeg
    api.assert_called_once_with('/recebimento/1/abc.jpg')
    http.assert_not_called()  # nao precisou cair no shared link


def test_foto_bytes_cai_no_shared_link_se_api_autenticada_falhar():
    """API retornou None → cai no shared link com User-Agent + raw."""
    jpeg = _jpeg_bytes()
    foto = SimpleNamespace(id=10, imagem_url='https://dropbox.com/x?dl=0',
                           imagem_storage_path='/recebimento/10/abc.jpg',
                           imagem=None)
    with patch('app.services.dropbox_storage.baixar', return_value=None), \
         patch('app.services.relatorio.requests.get',
               return_value=_resp(200, jpeg, 'image/jpeg')) as m:
        out = relatorio._foto_bytes(foto)
    assert out == jpeg
    _, kwargs = m.call_args
    assert 'Mozilla' in kwargs['headers']['User-Agent']


def test_foto_bytes_sem_storage_path_usa_shared_link():
    """Foto pre-storage_path (ainda assim com URL): so o shared link."""
    jpeg = _jpeg_bytes()
    foto = SimpleNamespace(id=2, imagem_url='https://dropbox.com/x?raw=1',
                           imagem_storage_path=None, imagem=None)
    with patch('app.services.dropbox_storage.baixar') as api, \
         patch('app.services.relatorio.requests.get',
               return_value=_resp(200, jpeg, 'image/jpeg')):
        out = relatorio._foto_bytes(foto)
    assert out == jpeg
    api.assert_not_called()


def test_foto_bytes_rejeita_html_de_preview_e_cai_no_fallback():
    """Dropbox respondeu HTML (status 200) no shared link. NAO pode passar
    isso pro fpdf2 — cai no BLOB legado (aqui simulado como bytes)."""
    html = b'<!DOCTYPE html><html><head>Dropbox preview</head></html>'
    blob_legado = _jpeg_bytes()
    foto = SimpleNamespace(id=2, imagem_url='https://dropbox.com/x?dl=0',
                           imagem_storage_path=None, imagem=blob_legado)
    with patch('app.services.relatorio.requests.get',
               return_value=_resp(200, html, 'text/html; charset=utf-8')):
        out = relatorio._foto_bytes(foto)
    assert out == blob_legado
    assert out != html


def test_foto_bytes_normaliza_url_pra_raw():
    """A URL `?dl=0` deve ser convertida pra raw antes do download."""
    jpeg = _jpeg_bytes()
    foto = SimpleNamespace(id=3,
                           imagem_url='https://www.dropbox.com/s/abc/f.jpg?dl=0',
                           imagem_storage_path=None, imagem=None)
    with patch('app.services.relatorio.requests.get',
               return_value=_resp(200, jpeg, 'image/jpeg')) as m:
        relatorio._foto_bytes(foto)
    url_chamada = m.call_args[0][0]
    assert 'dl=0' not in url_chamada
    assert 'raw=1' in url_chamada


def test_foto_bytes_erro_de_rede_nao_quebra():
    """Timeout/erro no shared link: loga e cai no BLOB, sem propagar."""
    blob_legado = _jpeg_bytes()
    foto = SimpleNamespace(id=4, imagem_url='https://dropbox.com/x?raw=1',
                           imagem_storage_path=None, imagem=blob_legado)
    with patch('app.services.relatorio.requests.get',
               side_effect=Exception('timeout')):
        out = relatorio._foto_bytes(foto)
    assert out == blob_legado

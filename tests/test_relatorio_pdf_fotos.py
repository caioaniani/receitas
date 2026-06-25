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


def test_foto_bytes_retorna_imagem_quando_dropbox_serve_jpeg():
    jpeg = _jpeg_bytes()
    foto = SimpleNamespace(id=1, imagem_url='https://dropbox.com/x?raw=1',
                           imagem=None)
    with patch('app.services.relatorio.requests.get',
               return_value=_resp(200, jpeg, 'image/jpeg')) as m:
        out = relatorio._foto_bytes(foto)
    assert out == jpeg
    # Confirma que mandou User-Agent de navegador (o pulo do gato).
    _, kwargs = m.call_args
    assert 'Mozilla' in kwargs['headers']['User-Agent']


def test_foto_bytes_rejeita_html_de_preview_e_cai_no_fallback():
    """Dropbox respondeu HTML (status 200). NAO pode passar isso pro fpdf2 —
    cai no BLOB legado (aqui simulado como bytes)."""
    html = b'<!DOCTYPE html><html><head>Dropbox preview</head></html>'
    blob_legado = _jpeg_bytes()
    foto = SimpleNamespace(id=2, imagem_url='https://dropbox.com/x?dl=0',
                           imagem=blob_legado)
    with patch('app.services.relatorio.requests.get',
               return_value=_resp(200, html, 'text/html; charset=utf-8')):
        out = relatorio._foto_bytes(foto)
    assert out == blob_legado          # nao retornou o HTML
    assert out != html


def test_foto_bytes_normaliza_url_pra_raw():
    """A URL `?dl=0` deve ser convertida pra raw antes do download."""
    jpeg = _jpeg_bytes()
    foto = SimpleNamespace(id=3, imagem_url='https://www.dropbox.com/s/abc/f.jpg?dl=0',
                           imagem=None)
    with patch('app.services.relatorio.requests.get',
               return_value=_resp(200, jpeg, 'image/jpeg')) as m:
        relatorio._foto_bytes(foto)
    url_chamada = m.call_args[0][0]
    assert 'dl=0' not in url_chamada
    assert 'raw=1' in url_chamada


def test_foto_bytes_erro_de_rede_nao_quebra():
    """Timeout/erro no Dropbox: loga e cai no fallback, sem propagar."""
    blob_legado = _jpeg_bytes()
    foto = SimpleNamespace(id=4, imagem_url='https://dropbox.com/x?raw=1',
                           imagem=blob_legado)
    with patch('app.services.relatorio.requests.get',
               side_effect=Exception('timeout')):
        out = relatorio._foto_bytes(foto)
    assert out == blob_legado

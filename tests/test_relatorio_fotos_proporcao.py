"""Regressao (25/06/2026): fotos do relatorio PDF saiam distorcidas.

`_render_fotos` desenhava cada foto com w E h fixos (45x35mm), entao fotos
com proporcao diferente (retrato de celular) ficavam espremidas — o dono
relatou "proporcao torta". O fix passa keep_aspect_ratio=True pro
pdf.image(), que ENCAIXA a foto na celula preservando a proporcao.
"""
import io
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from app.services import relatorio
from app.services.pdf import PadariaPDF


def _jpeg(w, h):
    b = io.BytesIO()
    Image.new('RGB', (w, h), (30, 90, 160)).save(b, 'JPEG')
    return b.getvalue()


def _foto(i):
    return SimpleNamespace(id=i, imagem_storage_path=f'/r/{i}.jpg',
                           imagem_url=None, imagem=None)


def test_render_fotos_preserva_proporcao(app):
    # Foto retrato (proporcao bem diferente de 45:35) — antes ficava torta.
    fotos = [_foto(1), _foto(2)]
    with app.app_context():
        pdf = PadariaPDF()
        pdf.add_page()
        with patch('app.services.dropbox_storage.baixar',
                   return_value=_jpeg(60, 120)), \
                patch.object(pdf, 'image') as img:
            relatorio._render_fotos(pdf, fotos)
    assert img.call_count == len(fotos)
    for chamada in img.call_args_list:
        assert chamada.kwargs.get('keep_aspect_ratio') is True, (
            'foto desenhada sem keep_aspect_ratio -> volta a distorcer')


def test_pdf_relatorio_com_fotos_de_proporcoes_variadas_gera(app):
    """Smoke: retrato, paisagem e quadrada no mesmo relatorio, sem estourar."""
    p = SimpleNamespace(id=7, data_entrega=None, tem_divergencia=False,
                        fotos=[_foto(1), _foto(2), _foto(3)], itens=[])
    pedidos = [{'p': p, 'linhas': [{'nome': 'Item', 'recebido': 1,
                                    'preco': 1.0, 'subtotal': 1.0}],
                'subtotal': 1.0}]
    totais = {'qtd_pedidos': 1, 'valor_total': 1.0, 'divergencias': 0}
    por_item = {'Item': {'quantidade': 1, 'recebido': 1, 'valor': 1.0}}
    from datetime import date
    formatos = [_jpeg(120, 60), _jpeg(60, 120), _jpeg(90, 90)]
    seq = iter(formatos * 3)
    with app.app_context():
        with patch('app.services.dropbox_storage.baixar',
                   side_effect=lambda _p: next(seq)):
            buf = relatorio.gerar_pdf_pedidos(
                'Loja Teste', date(2026, 6, 1), date(2026, 6, 25),
                pedidos, totais, por_item, incluir_fotos=True)
    assert buf.getvalue()[:5] == b'%PDF-'

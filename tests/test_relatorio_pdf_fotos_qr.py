"""Regressao: o PDF de relatorio precisa incluir as fotos do QR
(PedidoItemFoto: saida/entrega), nao so as de upload manual (FotoRecebimento).

Bug relatado pelo dono (25/06/2026): so as fotos que ele subiu manualmente
pela web apareciam no PDF; as tiradas no fluxo de conferencia por QR code
(entrega e recebimento) sumiam — porque o relatorio so olhava `p.fotos`
(FotoRecebimento) e nunca as PedidoItemFoto.
"""
import datetime
import io
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from app.services import relatorio


def _jpeg():
    b = io.BytesIO()
    Image.new('RGB', (80, 60), (50, 120, 200)).save(b, 'JPEG')
    return b.getvalue()


def _foto(id_, **kw):
    base = dict(id=id_, imagem_storage_path=f'/r/{id_}.jpg',
                imagem_url=None, imagem=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _pedido_com_fotos():
    foto_manual = _foto(1)  # FotoRecebimento (p.fotos)
    foto_saida = _foto(2, etapa='saida')
    foto_entrega = _foto(3, etapa='entrega')
    item = SimpleNamespace(nome_item='Croissant',
                           fotos_conferencia=[foto_saida, foto_entrega])
    return SimpleNamespace(id=232, data_entrega=None, tem_divergencia=False,
                           fotos=[foto_manual], itens=[item])


def test_fotos_conferencia_coleta_saida_e_entrega():
    p = _pedido_com_fotos()
    conf = relatorio._fotos_conferencia(p)
    ids = {f.id for f, _ in conf}
    assert ids == {2, 3}  # as duas etapas do QR
    legendas = {lg for _, lg in conf}
    assert 'Croissant (saida)' in legendas
    assert 'Croissant (entrega)' in legendas


def test_fotos_conferencia_vazio_sem_itens():
    p = SimpleNamespace(itens=[])
    assert relatorio._fotos_conferencia(p) == []


def test_pdf_renderiza_fotos_manual_e_qr():
    """O PDF chama _foto_bytes pras 3 fotos (1 manual + 2 QR)."""
    p = _pedido_com_fotos()
    pedidos = [{'p': p,
                'linhas': [{'nome': 'Croissant', 'recebido': 12,
                            'preco': 19.0, 'subtotal': 228.0}],
                'subtotal': 228.0}]
    totais = {'qtd_pedidos': 1, 'valor_total': 228.0, 'divergencias': 0}
    por_item = {'Croissant': {'quantidade': 12, 'recebido': 12,
                              'valor': 228.0}}

    baixados = []

    def _fake_baixar(path):
        baixados.append(path)
        return _jpeg()

    with patch('app.services.dropbox_storage.baixar', side_effect=_fake_baixar):
        buf = relatorio.gerar_pdf_pedidos(
            'Loja Teste', datetime.date(2026, 6, 1),
            datetime.date(2026, 6, 25), pedidos, totais, por_item,
            incluir_fotos=True)

    assert buf.getvalue()[:5] == b'%PDF-'
    # As 3 fotos foram baixadas (1 manual + 2 QR)
    assert set(baixados) == {'/r/1.jpg', '/r/2.jpg', '/r/3.jpg'}


def test_pdf_sem_incluir_fotos_nao_coleta_qr():
    """incluir_fotos=False: nao baixa NENHUMA foto (nem manual nem QR)."""
    p = _pedido_com_fotos()
    pedidos = [{'p': p,
                'linhas': [{'nome': 'Croissant', 'recebido': 12,
                            'preco': 19.0, 'subtotal': 228.0}],
                'subtotal': 228.0}]
    totais = {'qtd_pedidos': 1, 'valor_total': 228.0, 'divergencias': 0}
    por_item = {'Croissant': {'quantidade': 12, 'recebido': 12,
                              'valor': 228.0}}

    with patch('app.services.dropbox_storage.baixar') as m:
        relatorio.gerar_pdf_pedidos(
            'Loja Teste', datetime.date(2026, 6, 1),
            datetime.date(2026, 6, 25), pedidos, totais, por_item,
            incluir_fotos=False)
    m.assert_not_called()

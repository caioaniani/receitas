"""Helper de QR Code: gera PNG data URL pra embed em HTML.

Usado pelos templates de handshake (`pedidos/qr_saida.html`,
`driver/qr_entrega.html`) pra mostrar QR na tela sem precisar baixar.
"""
import base64
import io


def gerar_png_data_url(texto, box_size=8, border=2):
    """Gera QR code PNG e retorna data URL pra usar em <img src=...>.

    texto: o conteudo do QR (geralmente uma URL completa).
    box_size: tamanho de cada modulo em pixels (default 8 = ~250-300px total).
    border: modulos brancos ao redor do QR (default 2 = compacto).
    """
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(
        version=None,  # auto
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(texto)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    b64 = base64.b64encode(buf.getvalue()).decode('ascii')
    return f'data:image/png;base64,{b64}'

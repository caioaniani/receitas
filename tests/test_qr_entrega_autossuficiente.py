"""Tela de QR de entrega do motorista — autossuficiente (incidente 15/06/2026).

Pedido #202 (motorista Will, Loja Nebraska): ao tocar "Entregar agora", a
tela carregava mas "não concluía" — "tela preta sem o QR code". Causa: o
template puxava o CSS do Bootstrap do CDN jsdelivr com `<link>` render-
blocking no `<head>`; numa rede de loja ruim o navegador travava o primeiro
paint esperando o CDN responder. O QR já era data-URL inline; faltava o CSS
ser inline também.

Fix: `driver/qr_entrega.html` ficou 100% autossuficiente (sem CDN) e
`handshake/_base_mobile.html` carrega o Bootstrap de forma não render-
blocking (media=print/onload) com fallback inline.
"""
import re
from datetime import date


def _driver_em_transporte(loja, catalogo):
    """Cria driver autenticado + pedido em_transporte pronto pro qr_entrega."""
    from app.extensions import db
    from app.models import Driver, PedidoItem, PedidoLoja

    d = Driver(nome='Will', ativo=True, token='tok-will-legado', pin='0000')
    db.session.add(d)
    db.session.flush()
    p = PedidoLoja(loja_id=loja.id, status='em_transporte',
                   data_entrega=date.today(), driver_id=d.id)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, quantidade=15,
                              receita_id=catalogo['receita'].id))
    db.session.commit()
    return d, p


def test_qr_entrega_renderiza_com_qr_inline(app):
    """A tela do motorista mostra o QR (data-URL inline) e o link Voltar."""
    d, p = _driver_em_transporte(app)
    client = app.test_client()
    # Autentica o driver na sessão (PIN já passado no painel)
    with client.session_transaction() as s:
        s[f'driver_auth_{d.id}'] = True
    r = client.get(f'/driver/{d.token}/pedido/{p.id}/qr-entrega')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    # QR presente como data-URL inline (não depende de rede)
    assert 'data:image/png;base64,' in html
    assert f'Pedido #{p.id}' in html
    assert 'Voltar' in html


def test_qr_entrega_NAO_depende_de_cdn_render_blocking(app):
    """Regressão do incidente: a tela crítica de campo NÃO pode ter
    stylesheet externo render-blocking no <head>. O QR já é inline; o CSS
    também precisa ser (ou não existir dependência externa nenhuma)."""
    d, p = _driver_em_transporte(app)
    client = app.test_client()
    with client.session_transaction() as s:
        s[f'driver_auth_{d.id}'] = True
    r = client.get(f'/driver/{d.token}/pedido/{p.id}/qr-entrega')
    html = r.get_data(as_text=True)
    # Não pode haver NENHUM <link rel=stylesheet> apontando pra CDN externo
    # (jsdelivr/cdnjs/etc) — nem render-blocking nem não-blocking. A tela é
    # autossuficiente.
    links_externos = re.findall(
        r'<link[^>]+href=["\']https?://[^"\']+["\'][^>]*>', html)
    assert not links_externos, (
        f'tela de QR ainda depende de CSS externo: {links_externos}')
    # E o CSS essencial está inline
    assert '<style>' in html


def test_base_mobile_carrega_cdn_sem_bloquear_render():
    """O fluxo da loja (confirmar/sucesso/erro) ainda usa Bootstrap, mas o
    <link> do CDN NÃO pode bloquear o primeiro paint: precisa de
    media="print" + onload (técnica de carregamento assíncrono) e ter CSS
    inline de fallback pro caso de o CDN não responder."""
    import pathlib
    src = pathlib.Path('app/templates/handshake/_base_mobile.html').read_text()
    # Todo <link> de stylesheet pro CDN tem que ser não-blocking.
    for m in re.finditer(r'<link[^>]+cdn\.jsdelivr[^>]*>', src):
        tag = m.group(0)
        # Ou está dentro de <noscript> (fallback) ou tem media=print+onload
        assert ('media="print"' in tag and 'onload=' in tag), (
            f'link do CDN ainda é render-blocking: {tag}')
    # Fallback inline: alertas e form do PIN legíveis mesmo sem Bootstrap.
    assert '.alert-danger' in src
    assert '.form-control' in src
    assert '.btn-acao' in src


def test_base_mobile_tem_noscript_fallback():
    """Sem JS, o onload-swap não roda — precisa do <noscript> com link
    blocking pra garantir estilo nesse caso raro."""
    import pathlib
    src = pathlib.Path('app/templates/handshake/_base_mobile.html').read_text()
    assert '<noscript>' in src
    assert 'bootstrap' in src.lower()

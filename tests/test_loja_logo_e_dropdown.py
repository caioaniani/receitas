"""Logo da loja + dropdown "Produtos" por categoria (18/06/2026).

- comprimir_logo: preserva transparência (PNG fica PNG), SVG passa direto,
  raster opaco vira JPEG.
- Upload do logo: vai pro Dropbox, URL em AppConfig; header renderiza <img>.
- Dropdown "Produtos": lista categorias publicadas, cada uma linka pra
  /loja/#cat-<slug>; o id da seção na home usa o MESMO slug (pula certo).
"""
import io
from unittest.mock import patch


def _png_com_alpha(size=(600, 200)):
    from PIL import Image
    img = Image.new('RGBA', size, (255, 0, 0, 0))  # transparente
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def _jpeg(size=(600, 200)):
    from PIL import Image
    img = Image.new('RGB', size, 'red')
    buf = io.BytesIO()
    img.save(buf, format='JPEG')
    return buf.getvalue()


def _owner(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Dono', login='dono', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def _produto_pub(db, nome, categoria, preco=10.0):
    from app.models import Produto
    p = Produto(nome=nome, categoria=categoria, preco_site=preco,
                imagem_dropbox_url='https://x/y.jpg', ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


# ── comprimir_logo ──────────────────────────────────────────────────────

def test_comprimir_logo_preserva_png_alpha(app):
    from app.utils import comprimir_logo
    out, mime, ext = comprimir_logo(_png_com_alpha())
    assert mime == 'image/png' and ext == 'png'
    # Reabre e confere que continua com canal alpha
    from PIL import Image
    img = Image.open(io.BytesIO(out))
    assert img.mode in ('RGBA', 'LA', 'P')


def test_comprimir_logo_svg_passa_direto(app):
    from app.utils import comprimir_logo
    svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'
    out, mime, ext = comprimir_logo(svg)
    assert mime == 'image/svg+xml' and ext == 'svg'
    assert out == svg  # vetor sobe como veio


def test_comprimir_logo_jpeg_opaco_vira_jpeg(app):
    from app.utils import comprimir_logo
    out, mime, ext = comprimir_logo(_jpeg())
    assert mime == 'image/jpeg' and ext == 'jpg'


def test_comprimir_logo_vazio_levanta(app):
    import pytest

    from app.utils import comprimir_logo
    with pytest.raises(ValueError):
        comprimir_logo(b'')


# ── Upload / remoção do logo ────────────────────────────────────────────

def test_upload_logo_salva_url_em_appconfig(app):
    from app.models import AppConfig
    c = _owner(app)
    with patch('app.services.dropbox_storage.disponivel', return_value=True), \
         patch('app.services.dropbox_storage.upload_publico',
               return_value={'url': 'https://dropbox/logo.png?raw=1',
                             'storage_path': '/loja/logo.png',
                             'tamanho': 123}):
        r = c.post('/admin/loja-online/logo', data={
            'logo': (io.BytesIO(_png_com_alpha()), 'logo.png'),
        }, content_type='multipart/form-data', follow_redirects=False)
    assert r.status_code == 302
    with app.app_context():
        assert AppConfig.get('loja_logo_url') == 'https://dropbox/logo.png?raw=1'


def test_upload_logo_sem_arquivo_avisa(app):
    c = _owner(app)
    r = c.post('/admin/loja-online/logo', data={},
                content_type='multipart/form-data', follow_redirects=False)
    assert r.status_code == 302  # flash + redirect, não 500


def test_remover_logo_limpa_appconfig(app):
    from app.extensions import db
    from app.models import AppConfig
    c = _owner(app)
    with app.app_context():
        AppConfig.set('loja_logo_url', 'https://x/logo.png')
        db.session.commit()
    r = c.post('/admin/loja-online/logo/remover', follow_redirects=False)
    assert r.status_code == 302
    with app.app_context():
        # set(None) grava NULL → get() devolve None → header volta ao texto
        assert AppConfig.get('loja_logo_url') is None


def test_upload_logo_exige_owner(app):
    c = app.test_client()
    r = c.post('/admin/loja-online/logo', data={},
                content_type='multipart/form-data')
    assert r.status_code in (302, 401, 403)


# ── Header: logo <img> vs wordmark ──────────────────────────────────────

def test_header_mostra_wordmark_sem_logo(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    c = app.test_client()
    r = c.get('/loja/')
    assert r.status_code == 200
    assert b'Padaria' in r.data            # wordmark de texto
    assert b'<img src="https://dropbox' not in r.data


def test_header_mostra_logo_img_quando_setado(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    from app.extensions import db
    from app.models import AppConfig
    with app.app_context():
        AppConfig.set('loja_logo_url', 'https://dropbox/logo.png?raw=1')
        db.session.commit()
    c = app.test_client()
    r = c.get('/loja/')
    assert r.status_code == 200
    assert b'https://dropbox/logo.png?raw=1' in r.data


# ── Dropdown "Produtos" + âncoras ───────────────────────────────────────

def test_categorias_publicadas_nome_e_slug(app):
    from app.extensions import db
    from app.services import loja_catalogo
    with app.app_context():
        _produto_pub(db, 'Croissant', 'Viennoiserie')
        _produto_pub(db, 'Pão', 'Pães')
        cats = loja_catalogo.categorias_publicadas()
    nomes = {c['nome']: c['slug'] for c in cats}
    assert nomes.get('Pães') == 'paes'
    assert nomes.get('Viennoiserie') == 'viennoiserie'


def test_categorias_publicadas_respeita_ordem(app):
    from app.extensions import db
    from app.models import CategoriaSite
    from app.services import loja_catalogo
    with app.app_context():
        _produto_pub(db, 'A', 'Pães')
        _produto_pub(db, 'B', 'Bebidas')
        db.session.add(CategoriaSite(nome='Bebidas', ordem=0))
        db.session.add(CategoriaSite(nome='Pães', ordem=1))
        db.session.commit()
        cats = [c['nome'] for c in loja_catalogo.categorias_publicadas()]
    assert cats.index('Bebidas') < cats.index('Pães')


def test_header_dropdown_lista_categorias_com_anchor(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    from app.extensions import db
    c = app.test_client()
    with app.app_context():
        _produto_pub(db, 'Pão', 'Pães')
    r = c.get('/loja/')
    assert r.status_code == 200
    assert b'topo-dropdown' in r.data
    # Link do dropdown aponta pro anchor da home com o slug
    assert b'/loja/#cat-paes' in r.data


def test_home_secao_usa_slug_no_id(app, monkeypatch):
    """A seção da categoria na home tem id="cat-<slug>" — bate com o link
    do dropdown (sem isso o clique não pula)."""
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    from app.extensions import db
    c = app.test_client()
    with app.app_context():
        _produto_pub(db, 'Pão', 'Pães')
    r = c.get('/loja/')
    assert b'id="cat-paes"' in r.data
    # E o chip de navegação aponta pro mesmo anchor
    assert b'href="#cat-paes"' in r.data


def test_anchor_dropdown_bate_com_secao_home(app, monkeypatch):
    """Mesma categoria → MESMO slug no dropdown (#cat-X) e na seção
    (id=cat-X). Trava regressão de divergência de slug."""
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    from app.extensions import db
    c = app.test_client()
    with app.app_context():
        _produto_pub(db, 'Bolo', 'Doces & Sobremesas')
    r = c.get('/loja/')
    body = r.data
    assert b'/loja/#cat-doces-sobremesas' in body   # dropdown
    assert b'id="cat-doces-sobremesas"' in body      # seção

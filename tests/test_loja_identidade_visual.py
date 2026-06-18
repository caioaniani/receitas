"""Identidade visual "Caminho 1" (manual Panora) aplicada à loja.

Guarda contra regressão: fontes self-hosted presentes, @font-face + paleta
Caminho 1 no CSS, e o tema antigo (Georgia solta) não voltou.
"""
import os

_BASE = os.path.join(os.path.dirname(__file__), '..', 'app', 'static', 'loja')


def _css():
    with open(os.path.join(_BASE, 'loja.css'), encoding='utf-8') as fh:
        return fh.read()


def test_fontes_self_hosted_existem():
    fonts = os.path.join(_BASE, 'fonts')
    for f in ('fraunces-latin.woff2', 'funnel-sans-latin.woff2',
              'funnel-sans-italic-latin.woff2'):
        caminho = os.path.join(fonts, f)
        assert os.path.exists(caminho), f
        assert os.path.getsize(caminho) > 1000, f  # arquivo real, não vazio


def test_css_declara_font_face_e_aponta_pros_arquivos():
    css = _css()
    assert css.count('@font-face') >= 3
    assert "fonts/fraunces-latin.woff2" in css
    assert "fonts/funnel-sans-latin.woff2" in css
    assert "fonts/funnel-sans-italic-latin.woff2" in css
    assert "font-display: swap" in css   # evita texto invisível no load


def test_css_aplica_paleta_caminho_1():
    css = _css()
    assert "#F5F0E6" in css   # branco quente (Pantone 7499)
    assert "#323133" in css   # cinza escuro (Cool Gray 11)


def test_css_usa_variaveis_de_fonte():
    css = _css()
    assert "--fonte-titulo: 'Fraunces'" in css
    assert "--fonte-corpo: 'Funnel Sans'" in css
    assert "var(--fonte-titulo)" in css
    assert "var(--fonte-corpo)" in css


def test_css_sem_georgia_solta_do_tema_antigo():
    """Georgia/serif só pode aparecer como FALLBACK dentro da var, nunca
    como `font-family: 'Georgia'` solto (= tema pré-ID)."""
    css = _css()
    assert "font-family: 'Georgia'" not in css


def test_loja_csp_permite_fonte_self(app, monkeypatch):
    """A CSP da /loja precisa de font-src 'self' pra carregar os woff2
    auto-hospedados — sem isso a fonte é bloqueada e cai no fallback."""
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    c = app.test_client()
    r = c.get('/loja/')
    csp = r.headers.get('Content-Security-Policy', '')
    assert "font-src 'self'" in csp

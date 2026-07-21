"""Cardápio — bloco "Quem somos nós" (21/07/2026, pedido do dono).

A história da casa (escrita a partir do relato da fundação) aparece no
RODAPÉ do cardápio, antes das regras/métodos, nos TRÊS tipos (texto de
marca ≠ regras/preparo, que são só do atacado). AppConfig
`cardapio_quem_somos`, um parágrafo por linha; MESMO contrato do preparo:
chave AUSENTE = default no código; gravada VAZIA = escondido de propósito.
"""
import pytest

from app.extensions import db


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente, user):
    with cliente.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _receita(nome='Brioche', **kw):
    from app.models import Receita
    base = dict(nome=nome, categoria='Pães', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0, preco_venda=18.0,
                preco_loja=20.0)
    base.update(kw)
    r = Receita(**base)
    db.session.add(r)
    db.session.commit()
    return r


# ── Helper / contrato AppConfig ────────────────────────────────────────────

def test_default_sem_config(app):
    """Chave AUSENTE → default do código (a história real: família,
    pandemia, Abraço em Forma de Pão, fermentação natural)."""
    from app.blueprints.main.routes import _quem_somos
    paragrafos = _quem_somos()
    assert len(paragrafos) == 4
    texto = ' '.join(paragrafos)
    assert 'Abraço em Forma de Pão' in texto
    assert 'fermentação natural' in texto
    assert 'T65' in texto and 'Callebaut' in texto


def test_custom_e_vazio(app):
    from app.blueprints.main.routes import _quem_somos
    from app.models import AppConfig
    AppConfig.set('cardapio_quem_somos', 'Nossa história.\n\nSegunda linha.')
    db.session.commit()
    assert _quem_somos() == ['Nossa história.', 'Segunda linha.']

    # Gravada VAZIA = dono apagou de propósito → bloco some (≠ default)
    AppConfig.set('cardapio_quem_somos', '')
    db.session.commit()
    assert _quem_somos() == []


# ── Tela ───────────────────────────────────────────────────────────────────

def test_tela_mostra_nos_tres_tipos(app, admin_user, cliente):
    """Texto de marca: aparece no atacado E na loja/site (diferente das
    regras/preparo, que seguem só no atacado)."""
    _receita()
    _login(cliente, admin_user)
    for tipo in ('atacado', 'loja', 'site'):
        body = cliente.get(f'/cardapio?tipo={tipo}').get_data(as_text=True)
        assert 'Quem somos nós' in body, tipo
        assert 'Abraço em Forma de Pão' in body, tipo


def test_tela_historia_antes_das_regras_e_dos_produtos(app, admin_user,
                                                       cliente):
    """Default 21/07 (dono: "o rodapé venha para cima" — SUBSTITUI o
    "produtos para cima" de 20/07): história → regras → produtos. A
    posição é arrastável em Editar regras (tests/test_cardapio_ordem)."""
    from app.models import AppConfig
    _receita()
    AppConfig.set('cardapio_atacado_pedido_minimo', 'R$ 500,00')
    db.session.commit()
    _login(cliente, admin_user)
    body = cliente.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert body.index('Quem somos nós') < body.index('Regras do pedido')
    assert body.index('Regras do pedido') < body.index('Brioche')


def test_tela_esconde_quando_vazio(app, admin_user, cliente):
    from app.models import AppConfig
    _receita()
    AppConfig.set('cardapio_quem_somos', '')
    db.session.commit()
    _login(cliente, admin_user)
    body = cliente.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert 'Quem somos nós' not in body


# ── Tela de regras (edição) ────────────────────────────────────────────────

def test_form_regras_salva_quem_somos(app, admin_user, cliente):
    from app.models import AppConfig
    _login(cliente, admin_user)
    body = cliente.get('/admin/cardapio-atacado/regras').get_data(as_text=True)
    assert 'name="quem_somos"' in body
    assert 'Abraço em Forma de Pão' in body   # textarea pré-preenchida
    cliente.post('/admin/cardapio-atacado/regras',
                 data={'quem_somos': 'História editada pelo dono.'})
    assert AppConfig.get('cardapio_quem_somos') == \
        'História editada pelo dono.'


# ── PDF ────────────────────────────────────────────────────────────────────

def _cats():
    return {'Pães': [{'nome': 'Sourdough', 'preco_venda': 20.0,
                      'descricao': None, 'imagem_url': None,
                      'img_ref': None}]}


def test_pdf_cresce_com_quem_somos_em_todos_os_tipos(app):
    from app.blueprints.main.routes import CARDAPIO_QUEM_SOMOS_DEFAULT
    from app.services.cardapio_pdf import gerar_cardapio_pdf
    paragrafos = [ln for ln in CARDAPIO_QUEM_SOMOS_DEFAULT.splitlines()
                  if ln.strip()]
    for tipo in ('atacado', 'loja', 'site'):
        sem = gerar_cardapio_pdf(tipo, _cats(), [], quem_somos=None)
        com = gerar_cardapio_pdf(tipo, _cats(), [], quem_somos=paragrafos)
        assert com.startswith(b'%PDF')
        assert len(com) > len(sem) + 100, tipo


# ── Foto do bloco (21/07/2026) ─────────────────────────────────────────────

def test_foto_default_estatica_na_tela(app, admin_user, cliente):
    """Sem AppConfig, a tela usa a foto commitada da fachada
    (static/img/cardapio_quem_somos.jpg)."""
    _receita()
    _login(cliente, admin_user)
    body = cliente.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert 'img/cardapio_quem_somos.jpg' in body
    assert 'quem-somos-foto' in body


def test_foto_bytes_para_pdf_e_upload_override(app):
    """`_quem_somos_foto_bytes` lê o estático (JPEG real) e o upload via
    AppConfig (data URI) tem prioridade."""
    import base64

    from app.blueprints.main.routes import (
        _quem_somos_foto_bytes,
        _quem_somos_foto_src,
    )
    from app.models import AppConfig
    with app.test_request_context():
        b = _quem_somos_foto_bytes()
        assert b and b[:2] == b'\xff\xd8'   # JPEG magic
        fake = b'\xff\xd8meufake'
        AppConfig.set('cardapio_quem_somos_foto',
                      'data:image/jpeg;base64,'
                      + base64.b64encode(fake).decode())
        db.session.commit()
        assert _quem_somos_foto_bytes() == fake
        assert _quem_somos_foto_src().startswith('data:image/jpeg')
        # Remover ('') volta ao estático
        AppConfig.set('cardapio_quem_somos_foto', '')
        db.session.commit()
        assert _quem_somos_foto_bytes()[:2] == b'\xff\xd8'
        assert 'cardapio_quem_somos.jpg' in _quem_somos_foto_src()


def test_upload_foto_processa_e_remover_volta_ao_padrao(app, admin_user,
                                                        cliente):
    from io import BytesIO

    from PIL import Image

    from app.models import AppConfig
    _login(cliente, admin_user)
    buf = BytesIO()
    Image.new('RGB', (600, 500), (200, 120, 60)).save(buf, format='PNG')
    buf.seek(0)
    resp = cliente.post('/admin/cardapio-atacado/quem-somos-foto',
                        data={'qs_foto_arquivo': (buf, 'loja.png',
                                                  'image/png')},
                        content_type='multipart/form-data')
    assert resp.status_code in (302, 303)
    uri = AppConfig.get('cardapio_quem_somos_foto')
    assert uri and uri.startswith('data:image/jpeg;base64,')

    cliente.post('/admin/cardapio-atacado/quem-somos-foto/remover')
    assert AppConfig.get('cardapio_quem_somos_foto') == ''


def test_pdf_cresce_com_foto(app):
    from app.blueprints.main.routes import _quem_somos_foto_bytes
    from app.services.cardapio_pdf import gerar_cardapio_pdf
    with app.test_request_context():
        foto = _quem_somos_foto_bytes()
    paragrafos = ['Nossa história.']
    sem = gerar_cardapio_pdf('atacado', _cats(), [], quem_somos=paragrafos)
    com = gerar_cardapio_pdf('atacado', _cats(), [], quem_somos=paragrafos,
                             quem_somos_foto=foto)
    assert com.startswith(b'%PDF')
    assert len(com) > len(sem) + 10000     # a foto real tem ~190KB


# ── Rodapé com endereço (21/07/2026) ───────────────────────────────────────

def test_rodape_tela_tem_endereco(app, admin_user, cliente):
    _receita()
    _login(cliente, admin_user)
    body = cliente.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert 'Rua Ribeiro do Vale, 455' in body


# ── PDF formato mobile (21/07/2026) ────────────────────────────────────────

def test_pdf_mobile_pagina_estreita(app):
    """formato='mobile' gera página 120x213mm (MediaBox em pontos:
    340.16 x 603.78); o A4 segue 210x297 (595.28 x 841.89)."""
    from app.blueprints.main.routes import CARDAPIO_QUEM_SOMOS_DEFAULT
    from app.services.cardapio_pdf import gerar_cardapio_pdf
    paragrafos = [ln for ln in CARDAPIO_QUEM_SOMOS_DEFAULT.splitlines()
                  if ln.strip()]
    regras = [{'label': 'Pedido mínimo', 'valor': 'R$ 300,00'}]
    preparo = [{'label': 'Backup', 'valor': 'descongelar e assar.'}]
    mob = gerar_cardapio_pdf('atacado', _cats(), regras, preparo=preparo,
                             quem_somos=paragrafos, formato='mobile')
    a4 = gerar_cardapio_pdf('atacado', _cats(), regras, preparo=preparo,
                            quem_somos=paragrafos)
    assert mob.startswith(b'%PDF')
    assert b'340.16 603.78' in mob
    assert b'340.16 603.78' not in a4
    assert b'595.28 841.89' in a4


def test_pdf_mobile_com_foto_e_categorias_grandes(app):
    """Mobile com foto do quem-somos + categoria grande (12 itens, força
    quebra de página) gera sem exceção."""
    from app.blueprints.main.routes import (
        CARDAPIO_QUEM_SOMOS_DEFAULT,
        _quem_somos_foto_bytes,
    )
    from app.services.cardapio_pdf import gerar_cardapio_pdf
    with app.test_request_context():
        foto = _quem_somos_foto_bytes()
    paragrafos = [ln for ln in CARDAPIO_QUEM_SOMOS_DEFAULT.splitlines()
                  if ln.strip()]
    cats = {'Pães': [{'nome': f'Pão número {i}', 'preco_venda': 20.0,
                      'descricao': 'Farinha T65 e levain.',
                      'imagem_url': None, 'img_ref': None}
                     for i in range(12)]}
    pdf = gerar_cardapio_pdf('atacado', cats, [], quem_somos=paragrafos,
                             quem_somos_foto=foto, formato='mobile')
    assert pdf.startswith(b'%PDF')
    assert len(pdf) > 100000               # a foto (~190KB) está embutida


def test_rota_pdf_aceita_formato_mobile(app, admin_user, cliente):
    _receita()
    _login(cliente, admin_user)
    resp = cliente.get('/cardapio.pdf?tipo=atacado&formato=mobile')
    assert resp.status_code == 200
    assert resp.mimetype == 'application/pdf'
    assert 'mobile' in resp.headers['Content-Disposition']
    assert b'340.16 603.78' in resp.data
    # formato inválido cai no A4
    resp2 = cliente.get('/cardapio.pdf?tipo=atacado&formato=xyz')
    assert resp2.status_code == 200
    assert b'595.28 841.89' in resp2.data

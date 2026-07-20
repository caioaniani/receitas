"""Cardápio em PDF do servidor (19/07/2026) — substitui o window.print().

O navegador re-paginava o site de qualquer jeito (cards cortados no meio,
URL no rodapé). Agora: GET /cardapio.pdf?tipo= gera A4 diagramado no
servidor pela MESMA montagem de categorias da tela (fonte única).
"""
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from app.extensions import db


@pytest.fixture
def cliente(app):
    return app.test_client()


def _login(cliente, user):
    with cliente.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _jpeg_bytes(cor=(200, 120, 40)):
    from PIL import Image
    img = Image.new('RGB', (60, 40), cor)
    out = BytesIO()
    img.save(out, format='JPEG')
    return out.getvalue()


def _seed():
    from app.models import Produto, Receita
    r1 = Receita(nome='Sourdough Tradicional', categoria='Pães',
                 rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0,
                 preco_venda=20.0)
    r2 = Receita(nome='Brioche', categoria='Pães', rendimento_qtd=1,
                 rendimento_unidade='un', peso_base=100.0, preco_venda=18.0,
                 imagem_blob=_jpeg_bytes(), imagem_mimetype='image/jpeg')
    r3 = Receita(nome='Sem Preço', categoria='Pães', rendimento_qtd=1,
                 rendimento_unidade='un', peso_base=100.0)
    p1 = Produto(nome='Cesta Café da Manhã', categoria='Cestas', ativo=True,
                 preco_atacado=150.0)
    db.session.add_all([r1, r2, r3, p1])
    db.session.commit()
    return r1, r2, r3, p1


def test_rota_pdf_gera_documento(app, admin_user, cliente):
    _seed()
    _login(cliente, admin_user)
    resp = cliente.get('/cardapio.pdf?tipo=atacado')
    assert resp.status_code == 200
    assert resp.mimetype == 'application/pdf'
    assert resp.data.startswith(b'%PDF')
    assert len(resp.data) > 1500
    assert b'/Type /Page' in resp.data          # pelo menos 1 página real


def test_rota_pdf_exige_login(app, cliente):
    resp = cliente.get('/cardapio.pdf')
    assert resp.status_code in (302, 401)


def test_tela_troca_imprimir_por_exportar_pdf(app, admin_user, cliente):
    """A regra da casa (impressão de pedidos) vale aqui: NUNCA
    window.print(). O botão agora aponta pro PDF do servidor."""
    _seed()
    _login(cliente, admin_user)
    body = cliente.get('/cardapio?tipo=atacado').get_data(as_text=True)
    assert 'Exportar PDF' in body
    assert '/cardapio.pdf?tipo=atacado' in body
    assert 'window.print' not in body


def test_item_sem_foto_e_com_foto_blob(app, admin_user, cliente):
    """Item com BLOB entra no grid (bytes locais, sem rede); na regra do
    site, categoria com alguma foto vira grid com TODOS (sem foto =
    placeholder); receita sem preço fica fora — o PDF gera pra qualquer
    mistura."""
    _seed()
    _login(cliente, admin_user)
    resp = cliente.get('/cardapio.pdf?tipo=atacado')
    assert resp.status_code == 200
    # A foto do Brioche (JPEG do blob) foi embutida no PDF
    assert b'/Subtype /Image' in resp.data or b'DCTDecode' in resp.data


def test_download_de_foto_falhou_nao_derruba(app, admin_user, cliente):
    """Foto no Dropbox com rede fora: card sai sem foto, PDF gera igual."""
    from app.models import Receita
    from app.services import cardapio_pdf as svc
    _seed()
    r = Receita(nome='Pão com Foto Remota', categoria='Pães',
                rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0,
                preco_venda=25.0,
                imagem_dropbox_url='https://dl.dropbox.example/x.jpg?raw=1')
    db.session.add(r)
    db.session.commit()
    svc.limpar_cache_fotos()
    _login(cliente, admin_user)
    with patch('requests.get', side_effect=Exception('rede fora')):
        resp = cliente.get('/cardapio.pdf?tipo=atacado')
    assert resp.status_code == 200
    assert resp.data.startswith(b'%PDF')


def test_download_de_foto_dropbox_entra_no_pdf(app, admin_user, cliente):
    from app.models import Receita
    from app.services import cardapio_pdf as svc
    r = Receita(nome='Pão Dropbox', categoria='Pães', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0, preco_venda=25.0,
                imagem_dropbox_url='https://dl.dropbox.example/ok.jpg?raw=1')
    db.session.add(r)
    db.session.commit()
    svc.limpar_cache_fotos()
    _login(cliente, admin_user)
    fake = MagicMock(ok=True, content=_jpeg_bytes((30, 90, 160)))
    with patch('requests.get', return_value=fake) as rq:
        resp = cliente.get('/cardapio.pdf?tipo=atacado')
    assert resp.status_code == 200
    assert rq.call_count == 1
    assert b'DCTDecode' in resp.data            # JPEG embutido
    # 2ª geração usa o cache — sem novo download
    with patch('requests.get', return_value=fake) as rq2:
        cliente.get('/cardapio.pdf?tipo=atacado')
    assert rq2.call_count == 0


def test_regras_do_atacado_entram_no_pdf(app):
    """Com regra preenchida a capa ganha a caixa de regras — o documento fica
    ESTRITAMENTE maior que o mesmo cardápio sem regras (comparar `!=` seria
    vácuo: o fpdf2 embute data de criação e dois PDFs nunca são idênticos)."""
    from app.services.cardapio_pdf import gerar_cardapio_pdf
    cats = {'Pães': [{'nome': 'Sourdough', 'preco_venda': 20.0,
                      'imagem_url': None, 'img_ref': None}]}
    sem = gerar_cardapio_pdf('atacado', cats, [])
    com = gerar_cardapio_pdf('atacado', cats, [
        {'label': 'Pedido mínimo', 'valor': 'R$ 500,00'},
        {'label': 'Prazo para pedidos', 'valor': 'Pedir 48 horas antes'},
    ])
    assert len(com) > len(sem) + 50


def test_rota_le_regras_do_appconfig(app, admin_user, cliente):
    """A rota do PDF usa a MESMA fonte de regras da tela (AppConfig via
    _cardapio_categorias) — chave real, não hardcode."""
    from app.blueprints.main.routes import _cardapio_categorias
    from app.models import AppConfig
    _seed()
    AppConfig.set('cardapio_atacado_pedido_minimo', 'R$500,00')
    db.session.commit()
    with app.test_request_context():             # url_for das fotos internas
        _, regras = _cardapio_categorias('atacado')
    assert any(r['valor'] == 'R$500,00' for r in regras)


def test_altura_categoria_grid_e_lista():
    """Estimativa de altura: grid = ceil(n/3) fileiras de card alto;
    lista = ceil(n/2) fileiras de caixinha baixa (feedback 20/07 — usada pra
    manter a categoria inteira numa página)."""
    from app.services.cardapio_pdf import (
        _CARD_H,
        _GAP,
        _LINHA_H,
        _altura_categoria,
    )
    seis = [{'nome': f'x{i}', 'preco_venda': 1} for i in range(6)]
    # grid: 6 itens = 2 fileiras
    assert abs(_altura_categoria(seis, True)
               - (12.5 + 2 * (_CARD_H + _GAP))) < 0.01
    # lista: 6 itens = 3 fileiras
    assert abs(_altura_categoria(seis, False)
               - (12.5 + 3 * (_LINHA_H + 2) + 2)) < 0.01


def test_categoria_grande_gera_multipagina_sem_quebrar(app):
    """Muitas categorias/itens: PDF válido multipágina (o keep-together
    move categoria pra página limpa; nunca deve estourar)."""
    from app.services.cardapio_pdf import gerar_cardapio_pdf
    cats = {}
    for c in ('Pães', 'Viennoiserie', 'Doces', 'Salgados'):
        cats[c] = [{'nome': f'{c} {i}', 'preco_venda': 10.0 + i,
                    'imagem_url': None, 'img_ref': None} for i in range(6)]
    pdf = gerar_cardapio_pdf('atacado', cats, [])
    assert pdf.startswith(b'%PDF')
    # 4 categorias de 6 itens (placeholder, sem rede) → várias páginas
    assert pdf.count(b'/Type /Page') >= 2

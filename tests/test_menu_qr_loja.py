"""Cardápio da LOJA no QR Code: opao.online/menu (07/10/2026)."""
import pytest

from app.extensions import db
from app.models import Produto, Receita

pytestmark = pytest.mark.loja_host


def _itens():
    r = Receita(nome='Croissant QR', categoria='Viennoiserie', rendimento_qtd=1,
                rendimento_unidade='unidades', peso_base=1000, preco_loja=24.5,
                imagem_dropbox_url='https://www.dropbox.com/x.jpg?raw=1')
    sem_preco = Receita(nome='Sem preco loja', categoria='Pães', rendimento_qtd=1,
                        rendimento_unidade='unidades', peso_base=1000, preco_venda=9)
    db.session.add_all([r, sem_preco,
                        Produto(nome='Cesta QR', categoria='Cestas', ativo=True,
                                preco_loja=99.0)])
    db.session.commit()
    return r


def test_menu_redireciona_para_cardapio_da_loja(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    for caminho in ('/menu', '/menu/'):
        resp = app.test_client().get(caminho)
        assert resp.status_code == 302
        assert resp.headers['Location'].endswith('/loja/menu')


@pytest.mark.parametrize('visivel', ['1', '0'])
def test_cardapio_publico_anonimo_sem_barra_admin(app, monkeypatch, visivel):
    monkeypatch.setenv('LOJA_VISIVEL', visivel)  # vale em qualquer estado de cutover
    _itens()
    resp = app.test_client().get('/loja/menu')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'Croissant QR' in html and 'R$ 24,50' in html
    assert 'Cesta QR' in html and 'R$ 99,00' in html
    assert 'Sem preco loja' not in html
    assert 'Exportar PDF' not in html and 'Revisar fotos' not in html
    assert 'class="toolbar"' not in html


def test_foto_do_cardapio_responde_no_dominio_da_loja(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    r = _itens()
    resp = app.test_client().get(f'/cardapio-img/receita/{r.id}')
    assert resp.status_code == 302
    assert 'dropbox.com' in resp.headers['Location']


def test_admin_continua_bloqueado_no_dominio_da_loja(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')
    assert app.test_client().get('/cardapio?tipo=loja').status_code == 404

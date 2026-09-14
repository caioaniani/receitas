"""Capas reais de componentes no catálogo e no agendamento público de kits."""
from html.parser import HTMLParser

import pytest
from test_kits_checkout import ambiente as ambiente
from test_kits_checkout import kit as kit

from app.extensions import db
from app.models import EstoqueLoja, KitCafeItem, Produto, Receita

pytestmark = pytest.mark.loja_host
FOTOS = 'https://example.com/fotos-kit/'


class _Imagens(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.fotos = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'img':
            self.fotos.append(dict(attrs))


@pytest.fixture(params=['/loja/kits-cafe', '/loja/kits-cafe/{id}'])
def caminho(request, kit):
    return request.param.format(id=kit.id)


def _pagina(app, caminho):
    resposta = app.test_client().get(caminho)
    assert resposta.status_code == 200
    html = resposta.get_data(as_text=True)
    return html, [foto for foto in _Imagens(html).fotos
                  if foto.get('src', '').startswith(FOTOS)]


def test_mostra_capas_cadastradas_com_nome_do_componente(app, kit, caminho):
    receita = db.session.get(Receita, kit.itens[0].receita_id)
    produto = db.session.get(Produto, kit.itens[1].produto_id)
    receita.imagem_dropbox_url = FOTOS + 'croissant-atual.jpg'
    receita.imagem_url = FOTOS + 'croissant-antigo.jpg'
    produto.imagem_url = FOTOS + 'suco.jpg'
    db.session.commit()

    html, fotos = _pagina(app, caminho)
    assert [(foto['src'], foto['alt']) for foto in fotos] == [
        (receita.imagem_dropbox_url, receita.nome),
        (produto.imagem_url, produto.nome),
    ]
    assert 'Produtos que compõem o kit' in html


def test_sem_fotos_kit_continua_disponivel_para_escolher_e_comprar(app, kit, caminho):
    html, fotos = _pagina(app, caminho)
    assert not fotos
    assert kit.nome in html
    assert 'R$ 32,30 por kit' in html
    assert 'Produtos que compõem o kit' not in html
    if caminho == '/loja/kits-cafe':
        assert f'href="/loja/kits-cafe/{kit.id}"' in html
        assert 'Escolher este kit' in html
    else:
        assert 'id="kit-form"' in html
        assert 'id="kit-continuar"' in html


def test_galeria_nao_repete_capa_e_limita_a_tres_componentes(app, kit, loja, caminho):
    receita = db.session.get(Receita, kit.itens[0].receita_id)
    produto = db.session.get(Produto, kit.itens[1].produto_id)
    receita.imagem_url = produto.imagem_url = FOTOS + '1.jpg'
    for indice in range(2, 5):
        extra = Produto(nome=f'Item {indice}', ativo=True, site_ativo=True,
                        preco_site=5, imagem_url=f'{FOTOS}{indice}.jpg')
        db.session.add(extra)
        db.session.flush()
        kit.itens.append(KitCafeItem(kind='produto', produto_id=extra.id, quantidade=1))
        db.session.add(EstoqueLoja(loja_id=loja.id, produto_id=extra.id,
                                   quantidade=20, quantidade_reservada=0))
    db.session.commit()

    _, fotos = _pagina(app, caminho)
    assert [foto['src'] for foto in fotos] == [f'{FOTOS}{indice}.jpg' for indice in range(1, 4)]

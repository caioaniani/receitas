"""Kits na vitrine principal: publicação, navegação e composição preservadas."""
import re
from unittest.mock import Mock

import pytest
from test_kits_checkout import ambiente as ambiente
from test_kits_checkout import kit as kit
from test_kits_cliente_imagens import _secao_kits
from test_kits_opcoes_cliente import cenario as cenario
from test_kits_opcoes_cliente import plano as plano

from app.extensions import db
from app.models import CompraKit, EstoqueLoja, KitCafe, KitCafeItem, PedidoOnline, Produto

pytestmark = pytest.mark.loja_host


def test_home_exibe_kit_compravel_e_omite_pausados_ou_sem_composicao(app, kit, owner_user):
    pausado = KitCafe(nome='Kit pausado pelo owner', ativo=False, usuario_id=owner_user.id)
    pausado.itens.append(KitCafeItem(kind='receita', receita_id=kit.itens[0].receita_id,
                                    quantidade=1))
    invalido = KitCafe(nome='Kit sem composição válida', ativo=True, usuario_id=owner_user.id)
    db.session.add_all([pausado, invalido])
    db.session.commit()
    saldos = [(e.id, e.quantidade, e.quantidade_reservada) for e in EstoqueLoja.query.all()]
    resposta = app.test_client().get('/loja/')
    assert resposta.status_code == 200
    secao = _secao_kits(resposta.get_data(as_text=True))
    assert kit.nome in secao and f'href="/loja/kits-cafe/{kit.id}"' in secao
    assert 'R$ 32,30 por kit' in secao
    assert '2× Croissant simples' in secao
    assert '1× Suco de laranja 300 ml' in secao
    for oculto in [pausado, invalido]:
        assert oculto.nome not in secao
        assert f'href="/loja/kits-cafe/{oculto.id}"' not in secao
    assert [(e.id, e.quantidade, e.quantidade_reservada) for e in EstoqueLoja.query.all()] == saldos
    assert CompraKit.query.count() == PedidoOnline.query.count() == 0


@pytest.mark.parametrize('motivo', ['oculto', 'sem_preco'])
def test_home_nao_oferece_kit_se_item_deixou_de_ser_compravel(app, kit, motivo):
    produto = db.session.get(Produto, kit.itens[1].produto_id)
    if motivo == 'oculto':
        produto.site_ativo = False
    else:
        produto.preco_site = None
    db.session.commit()
    resposta = app.test_client().get('/loja/')
    assert resposta.status_code == 200
    assert f'href="/loja/kits-cafe/{kit.id}"' not in resposta.get_data(as_text=True)


def test_home_descreve_sucos_e_grupos_sem_duplicar_opcoes_como_itens_fixos(app, plano):
    kit, laranja, verde, almond, nutella, tradicional, integral, _ = plano
    resposta = app.test_client().get('/loja/')
    assert resposta.status_code == 200
    secao = _secao_kits(resposta.get_data(as_text=True))
    assert kit.nome in secao
    assert 'A partir de R$ 113,80 por kit' in secao
    assert f'1 suco à escolha: {laranja.nome} ou {verde.nome}' in secao
    assert f'1 croissant à escolha: {almond.nome} ou {nutella.nome}' in secao
    assert f'1 sourdough à escolha: {tradicional.nome} ou {integral.nome}' in secao
    for opcao in [laranja, verde, almond, nutella, tradicional, integral]:
        assert f'1× {opcao.nome}' not in secao
    assert '2× Croissant simples' in secao


def test_acesso_aos_kits_fica_somente_no_menu_produtos(app, kit):
    resposta = app.test_client().get('/loja/')
    assert resposta.status_code == 200
    html = resposta.get_data(as_text=True)
    _secao_kits(html)
    links = re.findall(r'<a\b[^>]*href="(?:/loja/)?#cat-kits-cafe"[^>]*>.*?</a>', html, re.S)
    assert len(links) == 1, 'Kits devem aparecer somente dentro do menu Produtos'
    assert 'role="menuitem"' in links[0]
    assert 'href="/loja/kits-cafe"' not in html


def test_catalogo_antigo_redireciona_301_sem_montar_kits_nem_calendarios(app, monkeypatch):
    from app.blueprints.loja import kits_routes
    from app.services import loja_leitura

    montar = Mock(side_effect=AssertionError('Redirecionamento não deve montar kits'))
    leitura = Mock(side_effect=AssertionError('Redirecionamento não deve carregar catálogo'))
    monkeypatch.setattr(kits_routes, 'cards_catalogo', montar)
    monkeypatch.setattr(loja_leitura, 'catalogo_em_lote', leitura)
    resposta = app.test_client().get('/loja/kits-cafe')
    assert resposta.status_code == 301
    assert resposta.location == '/loja/#cat-kits-cafe'
    montar.assert_not_called()
    leitura.assert_not_called()


@pytest.mark.parametrize('indisponivel', [False, True])
def test_compra_individual_continua_e_links_de_volta_apontam_para_home(
        app, kit, indisponivel):
    if indisponivel:
        db.session.get(Produto, kit.itens[1].produto_id).site_ativo = False
        db.session.commit()
    resposta = app.test_client().get(f'/loja/kits-cafe/{kit.id}')
    assert resposta.status_code == 200
    html = resposta.get_data(as_text=True)
    assert 'href="/loja/#cat-kits-cafe" class="kits-back"' in html
    assert 'href="/loja/kits-cafe"' not in html
    if indisponivel:
        assert re.search(r'<a[^>]+href="/loja/#cat-kits-cafe"[^>]*>Ver os kits disponíveis</a>', html)
    else:
        assert 'id="kit-form"' in html
        assert 'id="kit-continuar"' in html

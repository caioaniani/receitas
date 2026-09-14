"""Kits do owner: autorização, catálogo real, preço e composição fixada."""
import json
from decimal import Decimal

import pytest
from werkzeug.datastructures import MultiDict

from app.extensions import db
from app.models import KitCafe, KitCafeItem, Produto, Receita
from app.services import kits_cafe, loja_catalogo


def _cliente(app, usuario=None):
    cliente = app.test_client()
    if usuario:
        with cliente.session_transaction() as sessao:
            sessao['_user_id'] = str(usuario.id)
            sessao['_fresh'] = True
    return cliente


@pytest.fixture
def itens(app):
    receita = Receita(nome='Croissant simples', categoria='Croissants', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=100, site_ativo=True,
                      preco_site=Decimal('9.90'))
    produto = Produto(nome='Suco de laranja 300 ml', ativo=True, site_ativo=True,
                      preco_site=Decimal('12.50'))
    db.session.add_all([receita, produto])
    db.session.commit()
    return receita, produto


def _dados(itens, **extras):
    receita, produto = itens
    dados = {'nome': 'Café 1', 'descricao': 'Para começar a manhã', 'acao': 'publicar',
             f'qtd_receita_{receita.id}': '2', f'qtd_produto_{produto.id}': '1'}
    dados.update(extras)
    return dados


def _criar(app, owner_user, itens, **extras):
    cliente = _cliente(app, owner_user)
    resposta = cliente.post('/admin/kits-cafe/salvar', data=_dados(itens, **extras))
    assert resposta.status_code == 302
    return cliente, KitCafe.query.one()


@pytest.mark.parametrize('caminho', ['/admin/kits-cafe', '/admin/kits-cafe/novo',
                                    '/admin/kits-cafe/1'])
def test_admin_comum_nao_abre_area(app, admin_user, caminho):
    assert _cliente(app, admin_user).get(caminho).status_code == 403


@pytest.mark.parametrize('caminho', ['/admin/kits-cafe/salvar', '/admin/kits-cafe/1/pausar'])
def test_admin_comum_nao_pode_postar(app, admin_user, caminho):
    assert _cliente(app, admin_user).post(caminho, data={'nome': 'Inválido'}).status_code == 403
    assert KitCafe.query.count() == 0


def test_anonimo_precisa_login(app):
    resposta = _cliente(app).get('/admin/kits-cafe')
    assert resposta.status_code == 302
    assert 'login' in resposta.location


def test_owner_cria_com_preco_do_catalogo_e_autoria_real(app, owner_user, itens):
    cliente, kit = _criar(app, owner_user, itens, preco='0.01', valor_total='0.01',
                         usuario_id='9999', comp='{"999":1}')
    assert kit.ativo and kit.usuario_id == owner_user.id
    assert kits_cafe.preco_kit(kit) == Decimal('32.30')
    assert [item['qtd'] for item in kits_cafe.itens_do_kit(kit)] == [2, 1]
    assert kits_cafe.publicados() == [kit]
    resposta = cliente.get(f'/admin/kits-cafe/{kit.id}')
    assert resposta.status_code == 200
    html = resposta.get_data(as_text=True)
    assert '32,30' in html and 'csrf_token' in html
    assert 'sem renovação automática' in html.lower()


@pytest.mark.parametrize('quantidade', ['-1', '1.5', '1,5', '1000', '', 'abc', '1e2'])
def test_quantidade_invalida_recusada_sem_criar(app, owner_user, itens, quantidade):
    dados = _dados(itens)
    dados[f'qtd_receita_{itens[0].id}'] = quantidade
    resposta = _cliente(app, owner_user).post('/admin/kits-cafe/salvar', data=dados)
    assert resposta.status_code == 400
    assert KitCafe.query.count() == 0


def test_item_fora_do_catalogo_nao_publica(app, owner_user, itens):
    itens[0].site_ativo = False
    db.session.commit()
    resposta = _cliente(app, owner_user).post('/admin/kits-cafe/salvar', data=_dados(itens))
    assert resposta.status_code == 400
    assert 'não está mais à venda' in resposta.get_data(as_text=True)
    assert KitCafe.query.count() == 0


def test_item_esgotado_nao_aparece_no_editor_nem_publica(app, owner_user, itens, monkeypatch):
    receita, _ = itens
    original = loja_catalogo.tem_estoque_site
    monkeypatch.setattr(loja_catalogo, 'tem_estoque_site',
                        lambda kind, item_id, **kwargs: False if (kind, item_id) == ('receita', receita.id)
                        else original(kind, item_id, **kwargs))
    cliente = _cliente(app, owner_user)
    html = cliente.get('/admin/kits-cafe/novo').get_data(as_text=True)
    assert f'name="qtd_receita_{receita.id}"' not in html
    assert cliente.post('/admin/kits-cafe/salvar', data=_dados(itens)).status_code == 400
    assert KitCafe.query.count() == 0


def test_chave_e_quantidade_duplicada_nao_sao_sanitizadas_em_silencio(app, owner_user, itens):
    cliente = _cliente(app, owner_user)
    dados = MultiDict(_dados(itens))
    dados.add(f'qtd_receita_{itens[0].id}', '8')
    assert cliente.post('/admin/kits-cafe/salvar', data=dados).status_code == 400
    dados = _dados(itens, qtd_mp_1='1')
    assert cliente.post('/admin/kits-cafe/salvar', data=dados).status_code == 400
    assert KitCafe.query.count() == 0


def test_kit_vazio_nao_publica(app, owner_user):
    resposta = _cliente(app, owner_user).post('/admin/kits-cafe/salvar',
                                             data={'nome': 'Café', 'acao': 'publicar'})
    assert resposta.status_code == 400
    assert KitCafe.query.count() == 0


def test_rascunho_pausa_e_edicao_nao_reescrevem_precos_do_catalogo(app, owner_user, itens):
    cliente, kit = _criar(app, owner_user, itens, acao='rascunho')
    assert not kit.ativo and kits_cafe.publicados() == []
    dados = _dados(itens, kit_id=str(kit.id))
    dados[f'qtd_receita_{itens[0].id}'] = '3'
    assert cliente.post('/admin/kits-cafe/salvar', data=dados).status_code == 302
    assert kit.ativo and kits_cafe.preco_kit(kit) == Decimal('42.20')
    assert itens[0].preco_site == 9.90
    assert cliente.post(f'/admin/kits-cafe/{kit.id}/pausar').status_code == 302
    assert not kit.ativo and len(kit.itens) == 2


def test_preco_recalculado_e_item_pausado_oculta_kit_sem_vender_parcial(app, owner_user, itens):
    _, kit = _criar(app, owner_user, itens)
    itens[0].preco_site = 11
    db.session.commit()
    assert kits_cafe.preco_kit(kit) == Decimal('34.50')
    itens[0].site_ativo = False
    db.session.commit()
    assert kits_cafe.publicados() == []
    with pytest.raises(ValueError):
        kits_cafe.preco_kit(kit)


def test_owner_consegue_remover_item_que_saiu_de_catalogo(app, owner_user, itens):
    cliente, kit = _criar(app, owner_user, itens)
    itens[0].site_ativo = False
    db.session.commit()
    html = cliente.get(f'/admin/kits-cafe/{kit.id}').get_data(as_text=True)
    assert 'Indisponível no site' in html
    dados = _dados(itens, kit_id=str(kit.id))
    dados[f'qtd_receita_{itens[0].id}'] = '0'
    assert cliente.post('/admin/kits-cafe/salvar', data=dados).status_code == 302
    assert kits_cafe.preco_kit(kit) == Decimal('12.50')


def test_erro_de_edicao_preserva_kit_anterior(app, owner_user, itens):
    cliente, kit = _criar(app, owner_user, itens)
    dados = _dados(itens, kit_id=str(kit.id), nome='Outro')
    dados[f'qtd_receita_{itens[0].id}'] = '-2'
    assert cliente.post('/admin/kits-cafe/salvar', data=dados).status_code == 400
    db.session.expire_all()
    assert kit.nome == 'Café 1' and kits_cafe.preco_kit(kit) == Decimal('32.30')


def test_nomes_nao_executam_html_nos_templates(app, owner_user, itens):
    itens[0].nome = '<img src=x onerror=alert(1)>'
    db.session.commit()
    cliente, kit = _criar(app, owner_user, itens, nome='<script>alert(1)</script>')
    for caminho in ['/admin/kits-cafe', f'/admin/kits-cafe/{kit.id}']:
        html = cliente.get(caminho).get_data(as_text=True)
        assert '<script>alert(1)</script>' not in html
        assert '<img src=x onerror=alert(1)>' not in html
        assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html


def test_csrf_continua_obrigatorio(app, owner_user, itens):
    app.config['WTF_CSRF_ENABLED'] = True
    resposta = _cliente(app, owner_user).post('/admin/kits-cafe/salvar', data=_dados(itens))
    # O handler global redireciona formulários HTML quando o token expira.
    assert resposta.status_code == 302
    assert KitCafe.query.count() == 0


def test_menu_fixa_padrao_e_mudanca_exige_escolha_explicita(app, owner_user):
    from test_menu_configuravel import _menu, _pis
    menu, _ = _menu(db)
    menu.site_ativo = True
    db.session.commit()
    cliente = _cliente(app, owner_user)
    dados = {'nome': 'Café com minis', 'acao': 'publicar', f'qtd_produto_{menu.id}': '1',
             'comp': '{"999": 30}', 'preco': '0.01'}
    assert cliente.post('/admin/kits-cafe/salvar', data=dados).status_code == 302
    kit = KitCafe.query.one()
    comp_original = kits_cafe.itens_do_kit(kit)[0]['comp']
    assert comp_original == dict.fromkeys(_pis(menu), 5)
    assert kits_cafe.preco_kit(kit) == Decimal('45.00')
    menu.itens[0].quantidade, menu.itens[1].quantidade, menu.itens[2].quantidade = 10, 5, 0
    db.session.commit()
    dados['kit_id'] = str(kit.id)
    assert cliente.post('/admin/kits-cafe/salvar', data=dados).status_code == 302
    assert kits_cafe.itens_do_kit(kit)[0]['comp'] == comp_original
    html = cliente.get(f'/admin/kits-cafe/{kit.id}').get_data(as_text=True)
    assert 'Composição fixada' in html and 'Padrão atual' in html
    assert '5 × Mini 1' in html and '10 × Mini 1' in html
    dados[f'atualizar_menu_produto_{menu.id}'] = '1'
    assert cliente.post('/admin/kits-cafe/salvar', data=dados).status_code == 302
    assert kits_cafe.preco_kit(kit) == Decimal('35.00')
    assert kits_cafe.itens_do_kit(kit)[0]['comp'] != comp_original


def test_composicao_invalida_nao_vira_padrao_em_silencio(app, owner_user):
    from test_menu_configuravel import _menu
    menu, _ = _menu(db)
    menu.site_ativo = True
    kit = KitCafe(nome='Kit antigo', usuario_id=owner_user.id, ativo=True)
    kit.itens.append(KitCafeItem(kind='produto', produto_id=menu.id, quantidade=1,
                                 comp_json=json.dumps({'99999': 15})))
    db.session.add(kit)
    db.session.commit()
    assert kits_cafe.montar(kit)[1]
    assert kits_cafe.publicados() == []
    cliente = _cliente(app, owner_user)
    dados = {'kit_id': str(kit.id), 'nome': 'Kit antigo', 'acao': 'publicar',
             f'qtd_produto_{menu.id}': '1'}
    assert cliente.post('/admin/kits-cafe/salvar', data=dados).status_code == 400
    dados[f'atualizar_menu_produto_{menu.id}'] = '1'
    assert cliente.post('/admin/kits-cafe/salvar', data=dados).status_code == 302
    assert not kits_cafe.montar(kit)[1]


def test_composicao_corrompida_orienta_sem_erro500(app, owner_user, itens):
    cliente, kit = _criar(app, owner_user, itens)
    kit.itens[1].comp_json = '{json quebrado'
    db.session.commit()
    assert kits_cafe.montar(kit)[1]
    assert cliente.get(f'/admin/kits-cafe/{kit.id}').status_code == 200

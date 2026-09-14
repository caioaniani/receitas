"""O owner configura alternativas reais sem duplicar suco nos itens fixos."""
import re
from decimal import Decimal

import pytest
from werkzeug.datastructures import MultiDict

from app.extensions import db
from app.models import KitCafe, Produto, Receita
from app.services import kits_cafe


@pytest.fixture
def catalogo_sucos(app):
    pao = Receita(nome='Pão', rendimento_qtd=1, rendimento_unidade='un',
                  peso_base=100, site_ativo=True, preco_site=Decimal('3.50'))
    laranja = Produto(nome='Laranja 1 L', ativo=True, site_ativo=True,
                      preco_site=Decimal('50.00'))
    verde = Produto(nome='Verde 1 L', ativo=True, site_ativo=True,
                    preco_site=Decimal('52.00'))
    db.session.add_all([pao, laranja, verde])
    db.session.commit()
    return pao, laranja, verde


def cliente_owner(app, owner_user):
    cliente = app.test_client()
    with cliente.session_transaction() as sessao:
        sessao['_user_id'] = str(owner_user.id)
        sessao['_fresh'] = True
    return cliente


def dados(catalogo):
    pao, laranja, verde = catalogo
    return {'nome': 'Kit com suco', 'acao': 'publicar',
            f'qtd_receita_{pao.id}': '3', 'configurar_sucos': '1',
            'suco_ids': [str(laranja.id), str(verde.id)]}


def test_owner_salva_reabre_e_preserva_opcoes_em_edicao(app, owner_user, catalogo_sucos):
    cliente = cliente_owner(app, owner_user)
    form = dados(catalogo_sucos)
    resposta = cliente.post('/admin/kits-cafe/salvar', data=form)
    assert resposta.status_code == 302
    kit = KitCafe.query.one()
    assert len(kit.sucos) == 2
    assert kits_cafe.preco_kit(kit) == Decimal('60.50')
    html = cliente.get(resposta.location).get_data(as_text=True)
    assert 'Sucos à escolha do cliente' in html
    assert len(re.findall(r'<input[^>]+name="suco_ids"', html)) == 2
    form['kit_id'] = str(kit.id)
    form['descricao'] = 'Uma escolha para todas as datas'
    assert cliente.post('/admin/kits-cafe/salvar', data=form).status_code == 302
    assert len(kit.sucos) == 2
    assert '1 suco à escolha:' in cliente.get('/admin/kits-cafe').get_data(as_text=True)
    # Cliente de integração legado não apaga alternativas por omitir o bloco.
    form.pop('configurar_sucos')
    form.pop('suco_ids')
    assert cliente.post('/admin/kits-cafe/salvar', data=form).status_code == 302
    assert len(kit.sucos) == 2


@pytest.mark.parametrize('ids', [['0', '2'], ['-1', '2'], ['1.0', '2'], ['1', '1'], ['1']])
def test_opcoes_invalidas_nao_criam_kit(app, owner_user, catalogo_sucos, ids):
    form = dados(catalogo_sucos)
    form['suco_ids'] = ids
    assert cliente_owner(app, owner_user).post('/admin/kits-cafe/salvar', data=form).status_code == 400
    assert KitCafe.query.count() == 0


def test_nao_duplica_suco_fixo_e_alternativo(app, owner_user, catalogo_sucos):
    form = dados(catalogo_sucos)
    form[f'qtd_produto_{catalogo_sucos[1].id}'] = '1'
    assert cliente_owner(app, owner_user).post('/admin/kits-cafe/salvar', data=form).status_code == 400
    assert KitCafe.query.count() == 0


def test_suco_desativado_exibe_erro_e_preserva_escolhas(app, owner_user, catalogo_sucos):
    cliente = cliente_owner(app, owner_user)
    form = dados(catalogo_sucos)
    assert cliente.post('/admin/kits-cafe/salvar', data=form).status_code == 302
    kit = KitCafe.query.one()
    catalogo_sucos[2].site_ativo = False
    db.session.commit()
    html = cliente.get(f'/admin/kits-cafe/{kit.id}').get_data(as_text=True)
    assert 'Indisponível, revise o cadastro' in html
    form['kit_id'] = str(kit.id)
    assert cliente.post('/admin/kits-cafe/salvar', data=form).status_code == 400
    assert len(kit.sucos) == 2


def test_owner_remove_grupo_explicitamente(app, owner_user, catalogo_sucos):
    cliente = cliente_owner(app, owner_user)
    form = dados(catalogo_sucos)
    assert cliente.post('/admin/kits-cafe/salvar', data=form).status_code == 302
    kit = KitCafe.query.one()
    form['kit_id'] = str(kit.id)
    form['suco_ids'] = []
    assert cliente.post('/admin/kits-cafe/salvar', data=form).status_code == 302
    assert not kit.sucos
    assert kits_cafe.preco_kit(kit) == Decimal('10.50')


def test_admin_nao_configura_opcoes(app, admin_user, catalogo_sucos):
    cliente = cliente_owner(app, admin_user)
    assert cliente.post('/admin/kits-cafe/salvar', data=MultiDict(dados(catalogo_sucos))).status_code == 403
    assert KitCafe.query.count() == 0


@pytest.mark.parametrize('acoes', [[], [''], ['desconhecida'], ['publicar', 'rascunho']])
def test_acao_ausente_ou_ambigua_nao_cria_kit(app, owner_user, catalogo_sucos, acoes):
    form = MultiDict(dados(catalogo_sucos))
    form.setlist('acao', acoes)
    resposta = cliente_owner(app, owner_user).post('/admin/kits-cafe/salvar', data=form)
    assert resposta.status_code == 400
    assert 'Escolha salvar e publicar' in resposta.get_data(as_text=True)
    assert KitCafe.query.count() == 0


def test_acao_ausente_nao_pausa_kit_publicado(app, owner_user, catalogo_sucos):
    cliente = cliente_owner(app, owner_user)
    form = dados(catalogo_sucos)
    assert cliente.post('/admin/kits-cafe/salvar', data=form).status_code == 302
    kit = KitCafe.query.one()
    form['kit_id'] = str(kit.id)
    form['nome'] = 'Nome que não deve ser salvo'
    form.pop('acao')
    assert cliente.post('/admin/kits-cafe/salvar', data=form).status_code == 400
    db.session.refresh(kit)
    assert kit.ativo is True
    assert kit.nome == 'Kit com suco'

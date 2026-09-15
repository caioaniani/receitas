"""Capas escolhidas pelo owner: apenas fotos da composição, sem efeitos na venda."""
from decimal import Decimal

import pytest
from flask import g
from test_kits_opcoes_admin import opcoes as opcoes
from test_kits_sucos_owner import catalogo_sucos as catalogo_sucos
from test_kits_sucos_owner import cliente_owner, dados
from werkzeug.datastructures import MultiDict

from app.extensions import db
from app.models import CompraKit, KitCafe, KitCafeFoto, PedidoOnline, Produto, Receita
from app.services import kits_cafe

FOTOS = 'https://example.com/fotos-kit/'


def _alvo(chave):
    kind, item_id = chave.split(':')
    return db.session.get(Receita if kind == 'receita' else Produto, int(item_id))


@pytest.fixture
def catalogo_fotos(catalogo_sucos, opcoes):
    pao, laranja, verde = catalogo_sucos
    cookie = Produto(nome='Cookie do kit', ativo=True, site_ativo=True, preco_site=15)
    avulso = Produto(nome='Produto de outro kit', ativo=True, site_ativo=True, preco_site=8)
    db.session.add_all([cookie, avulso])
    db.session.flush()
    itens = [pao, laranja, verde, cookie, avulso,
             *[_alvo(chave) for grupo in opcoes.values() for chave in grupo]]
    for indice, item in enumerate(itens):
        item.imagem_url = f'{FOTOS}{indice}.jpg'
    db.session.commit()
    return {'catalogo': catalogo_sucos, 'opcoes': opcoes, 'pao': pao,
            'cookie': cookie, 'avulso': avulso, 'laranja': laranja, 'verde': verde}


def _form(catalogo, fotos=()):
    form = {**dados(catalogo['catalogo']), 'configurar_opcoes': '1',
            f'qtd_produto_{catalogo["cookie"].id}': '2', 'configurar_fotos': '1',
            **{f'opcao_{grupo}': valores for grupo, valores in catalogo['opcoes'].items()}}
    form.update({f'foto_{ordem}': fotos[ordem - 1] if ordem <= len(fotos) else ''
                 for ordem in range(1, 4)})
    return form


def _fotos(kit):
    return [(foto.ordem, foto.kind, foto.receita_id or foto.produto_id) for foto in kit.fotos]


def _conteudo(kit):
    return {'nome': kit.nome, 'ativo': kit.ativo,
            'itens': [(i.kind, i.receita_id, i.produto_id, i.quantidade, i.comp_json)
                      for i in kit.itens],
            'sucos': [s.produto_id for s in kit.sucos],
            'opcoes': [(o.grupo, o.kind, o.receita_id, o.produto_id) for o in kit.opcoes],
            'fotos': _fotos(kit), 'preco': kits_cafe.preco_kit(kit)}


def _criar(app, owner_user, catalogo, fotos=()):
    cliente = cliente_owner(app, owner_user)
    form = _form(catalogo, fotos)
    resposta = cliente.post('/admin/kits-cafe/salvar', data=form)
    assert resposta.status_code == 302, resposta.get_data(as_text=True)
    kit = KitCafe.query.one()
    form['kit_id'] = str(kit.id)
    return cliente, kit, form


def test_novo_kit_aceita_fotos_de_suco_e_opcoes_sem_somar_alternativas(
        app, owner_user, catalogo_fotos):
    catalogo = catalogo_fotos
    chaves = [catalogo['opcoes']['sourdough'][1], catalogo['opcoes']['croissant'][1],
              f'produto:{catalogo["verde"].id}']
    cliente, kit, _ = _criar(app, owner_user, catalogo, chaves)
    assert _fotos(kit) == [(ordem, chave.split(':')[0], int(chave.split(':')[1]))
                           for ordem, chave in enumerate(chaves, start=1)]
    assert KitCafeFoto.query.count() == 3
    assert kits_cafe.preco_kit(kit) == Decimal('159.50')
    assert [item.quantidade for item in kit.itens] == [3, 2]
    assert len(kit.sucos) == 2 and len(kit.opcoes) == 4
    assert CompraKit.query.count() == PedidoOnline.query.count() == 0
    html = cliente.get(f'/admin/kits-cafe/{kit.id}').get_data(as_text=True)
    assert 'name="configurar_fotos"' in html
    assert all(f'name="foto_{ordem}"' in html for ordem in range(1, 4))


def test_owner_reordena_fotos_em_slots_existentes_sem_duplicar_registros(
        app, owner_user, catalogo_fotos):
    pao, cookie = catalogo_fotos['pao'], catalogo_fotos['cookie']
    primeira, segunda = f'receita:{pao.id}', f'produto:{cookie.id}'
    cliente, kit, form = _criar(app, owner_user, catalogo_fotos, [primeira, segunda])
    antes = _conteudo(kit)
    for ordem in [(segunda, primeira), (primeira, segunda), (segunda, primeira)]:
        form.update(foto_1=ordem[0], foto_2=ordem[1])
        assert cliente.post('/admin/kits-cafe/salvar', data=form).status_code == 302
        db.session.expire_all()
        assert _fotos(kit) == [(i, chave.split(':')[0], int(chave.split(':')[1]))
                               for i, chave in enumerate(ordem, start=1)]
        assert KitCafeFoto.query.count() == 2
        depois = _conteudo(kit)
        assert {k: v for k, v in depois.items() if k != 'fotos'} == {
            k: v for k, v in antes.items() if k != 'fotos'}


def test_owner_pode_voltar_ao_modo_automatico(app, owner_user, catalogo_fotos):
    cliente, kit, form = _criar(app, owner_user, catalogo_fotos,
                                [f'receita:{catalogo_fotos["pao"].id}'])
    preco = kits_cafe.preco_kit(kit)
    form.update(foto_1='', foto_2='', foto_3='')
    assert cliente.post('/admin/kits-cafe/salvar', data=form).status_code == 302
    db.session.expire_all()
    assert not kit.fotos and KitCafeFoto.query.count() == 0
    assert kits_cafe.preco_kit(kit) == preco


def test_cliente_legado_sem_bloco_preserva_fotos_e_remove_apenas_item_que_saiu(
        app, owner_user, catalogo_fotos):
    pao, cookie = catalogo_fotos['pao'], catalogo_fotos['cookie']
    cliente, kit, form = _criar(app, owner_user, catalogo_fotos,
                                [f'produto:{cookie.id}', f'receita:{pao.id}'])
    for chave in ['configurar_fotos', 'foto_1', 'foto_2', 'foto_3']:
        form.pop(chave)
    fotos_antes = _fotos(kit)
    assert cliente.post('/admin/kits-cafe/salvar', data=form).status_code == 302
    db.session.expire_all()
    assert _fotos(kit) == fotos_antes
    form[f'qtd_produto_{cookie.id}'] = '0'
    assert cliente.post('/admin/kits-cafe/salvar', data=form).status_code == 302
    db.session.expire_all()
    assert [(f.kind, f.receita_id, f.produto_id) for f in kit.fotos] == [
        ('receita', pao.id, None)]


@pytest.mark.parametrize('valor', ['receita:999999999', 'mp:1', 'receita:-1',
                                  'receita:1.5', 'produto', 'https://example.com/foto.jpg'])
def test_referencia_invalida_nao_cria_kit_ou_fotos(app, owner_user, catalogo_fotos, valor):
    resposta = cliente_owner(app, owner_user).post(
        '/admin/kits-cafe/salvar', data=_form(catalogo_fotos, [valor]))
    assert resposta.status_code == 400
    assert KitCafe.query.count() == KitCafeFoto.query.count() == 0


@pytest.mark.parametrize('falha', ['alheio', 'sem_foto', 'duplicado', 'mesma_url',
                                   'removido_neste_post', 'campo_repetido'])
def test_foto_invalida_recusa_edicao_inteira_sem_perder_capa_anterior(
        app, owner_user, catalogo_fotos, falha):
    catalogo = catalogo_fotos
    pao, cookie = catalogo['pao'], catalogo['cookie']
    cliente, kit, form = _criar(app, owner_user, catalogo, [f'receita:{pao.id}'])
    anterior = _conteudo(kit)
    form = MultiDict(form)
    form['nome'] = 'Nome que não pode ser salvo'
    form[f'qtd_produto_{cookie.id}'] = '5'
    if falha == 'alheio':
        form['foto_1'] = f'produto:{catalogo["avulso"].id}'
    elif falha == 'sem_foto':
        cookie.imagem_url = cookie.imagem_dropbox_url = None
        db.session.commit()
        form['foto_2'] = f'produto:{cookie.id}'
    elif falha == 'duplicado':
        form['foto_2'] = form['foto_1']
    elif falha == 'mesma_url':
        cookie.imagem_dropbox_url = pao.imagem_url
        db.session.commit()
        form['foto_2'] = f'produto:{cookie.id}'
    elif falha == 'removido_neste_post':
        form[f'qtd_receita_{pao.id}'] = '0'
    else:
        form.add('foto_1', f'produto:{cookie.id}')
    assert cliente.post('/admin/kits-cafe/salvar', data=form).status_code == 400
    db.session.expire_all()
    assert _conteudo(kit) == anterior
    assert KitCafeFoto.query.count() == 1
    assert CompraKit.query.count() == PedidoOnline.query.count() == 0


def test_admin_sem_owner_nao_muda_as_fotos(app, owner_user, admin_user, catalogo_fotos):
    _, kit, form = _criar(app, owner_user, catalogo_fotos,
                           [f'receita:{catalogo_fotos["pao"].id}'])
    anterior = _conteudo(kit)
    form['foto_1'] = f'produto:{catalogo_fotos["cookie"].id}'
    g.pop('_login_user', None)  # A fixture compartilha app_context entre clientes.
    assert cliente_owner(app, admin_user).post(
        '/admin/kits-cafe/salvar', data=form).status_code == 403
    db.session.expire_all()
    assert _conteudo(kit) == anterior


def test_anonimo_nao_configura_fotos(app, catalogo_fotos):
    resposta = app.test_client().post('/admin/kits-cafe/salvar', data=_form(catalogo_fotos))
    assert resposta.status_code == 302 and 'login' in resposta.location
    assert KitCafe.query.count() == KitCafeFoto.query.count() == 0

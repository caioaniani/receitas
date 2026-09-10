"""Edição colaborativa por unidade, autoria e confirmação de exclusão."""
import pytest

from app.extensions import db
from app.models import ChecklistEdicao, ChecklistItemModelo, ChecklistResposta, Loja
from app.services import checklist_loja
from tests.test_checklist_responsaveis import _pessoa


@pytest.fixture
def contexto(app, loja):
    chefe = _pessoa('ChefeEditor', loja)
    outra = Loja(nome='Outra', ativa=True)
    item = ChecklistItemModelo(tipo='abertura', texto='Conferir café', exige_foto=False)
    db.session.add_all([outra, item])
    db.session.commit()
    client = app.test_client()
    client.post('/auth/login', data={'login': chefe.usuario.login, 'senha': '123'})
    return client, loja, outra, item, chefe


def enviar(ctx, acao, **dados):
    client, loja, _, item, _ = ctx
    atual = next((i for i in checklist_loja.itens_para(loja.id, 'abertura') if i.id == item.id), None)
    return client.post('/checklist/itens', data=dict(
        loja=loja.id, tipo='abertura', item_id=item.id,
        versao=atual.versao if atual else '', acao=acao, **dados))


def test_editar_global_apenas_na_loja_e_auditar(contexto):
    _, loja, outra, item, chefe = contexto
    assert enviar(contexto, 'editar', texto='Conferir espresso', setor='Café').status_code == 200
    assert checklist_loja.itens_para(loja.id, 'abertura')[0].texto == 'Conferir espresso'
    assert checklist_loja.itens_para(outra.id, 'abertura')[0].texto == 'Conferir café'
    assert db.session.get(ChecklistItemModelo, item.id).texto == 'Conferir café'
    log = ChecklistEdicao.query.one()
    assert log.usuario_id == chefe.usuario_id
    assert log.antes['texto'] == 'Conferir café'
    assert log.depois['texto'] == 'Conferir espresso'


def test_exclusao_exige_senha_propria_e_preserva_historico(contexto, admin_user):
    _, loja, outra, item, chefe = contexto
    checklist_loja.registrar(loja, 'abertura', chefe.usuario_id, {item.id: {'ok': True}})
    admin_user.set_senha('senha-do-admin')
    db.session.commit()
    for senha in ('', 'errada', 'senha-do-admin'):
        assert enviar(contexto, 'excluir', senha=senha).status_code == 403
        assert checklist_loja.itens_para(loja.id, 'abertura')
    assert enviar(contexto, 'excluir', senha='123').status_code == 200
    assert not checklist_loja.itens_para(loja.id, 'abertura')
    assert checklist_loja.itens_para(outra.id, 'abertura')
    assert ChecklistResposta.query.one().item_texto == 'Conferir café'
    assert ChecklistEdicao.query.one().depois is None
    assert checklist_loja.tipos_configurados(loja.id) == {}


def test_nao_edita_outra_loja_nem_item_estranho(contexto):
    client, loja, outra, _, _ = contexto
    assert client.post('/checklist/itens', data=dict(loja=outra.id, tipo='abertura',
        acao='novo', texto='Proibido')).status_code == 403
    estranho = ChecklistItemModelo(loja_id=outra.id, tipo='abertura', texto='Outra')
    db.session.add(estranho)
    db.session.commit()
    assert client.post('/checklist/itens', data=dict(loja=loja.id, tipo='abertura',
        acao='editar', item_id=estranho.id, versao='0', texto='Proibido')).status_code == 409
    assert ChecklistEdicao.query.count() == 0


def test_novo_local_e_checklist_vazio_continua_acessivel(contexto):
    client, loja, outra, _, _ = contexto
    assert enviar(contexto, 'excluir', senha='123').status_code == 200
    pagina = client.get(f'/checklist/preencher?loja={loja.id}&tipo=abertura')
    assert pagina.status_code == 200
    assert b'adicionar-ponto' in pagina.data
    assert enviar(contexto, 'novo', texto='Conferir forno', setor='Cozinha').status_code == 200
    assert checklist_loja.itens_para(loja.id, 'abertura')[0].texto == 'Conferir forno'
    assert len(checklist_loja.itens_para(outra.id, 'abertura')) == 1


def test_conflito_de_edicao_e_formulario_aberto(contexto):
    client, loja, _, item, _ = contexto
    versao = checklist_loja.itens_para(loja.id, 'abertura')[0].versao
    assert enviar(contexto, 'editar', texto='Novo texto').status_code == 200
    assert client.post('/checklist/itens', data=dict(loja=loja.id, tipo='abertura',
        acao='editar', item_id=item.id, versao=versao, texto='Sobrescrever')).status_code == 409
    result = client.post('/checklist/preencher', data={
        'loja': loja.id, 'tipo': 'abertura', 'revisao_itens': '1',
        'itens_presentes': str(item.id), f'versao_{item.id}': versao, f'ok_{item.id}': 'ok'})
    assert result.status_code == 409
    assert ChecklistResposta.query.count() == 0


def test_preenchedor_sem_lideranca_nao_edita(contexto):
    client, loja, _, _, _ = contexto
    pessoa = _pessoa('AtendenteSimples', loja, cargo='ATENDENTE')
    client.get('/auth/logout')
    client.post('/auth/login', data={'login': pessoa.usuario.login, 'senha': '123'})
    assert client.post('/checklist/itens', data=dict(loja=loja.id, tipo='abertura',
        acao='novo', texto='Proibido')).status_code == 403


def test_admin_excluir_modelo_personalizado_preserva_vinculo(contexto, admin_user):
    client, _, _, item, _ = contexto
    assert enviar(contexto, 'editar', texto='Revisado').status_code == 200
    client.get('/auth/logout')
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'})
    response = client.post('/checklist/config', data={'acao': 'excluir', 'item_id': item.id})
    assert response.status_code == 302
    assert db.session.get(ChecklistItemModelo, item.id).ativo is False


def test_resposta_posterior_guarda_texto_revisado(contexto):
    _, loja, _, item, chefe = contexto
    assert enviar(contexto, 'editar', texto='Revisado').status_code == 200
    checklist_loja.registrar(loja, 'abertura', chefe.usuario_id, {item.id: {'ok': True}})
    assert ChecklistResposta.query.one().item_texto == 'Revisado'

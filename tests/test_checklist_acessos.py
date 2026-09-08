"""Concessão de checklist por vínculo, sem promoção de perfil ou alteração do RH."""
import json

import pytest
from flask import g
from flask.testing import FlaskClient

from app.extensions import db
from app.models import (
    AuditLog,
    ChecklistItemModelo,
    ChecklistPreenchimento,
    ChecklistResponsavel,
    Funcionario,
    Loja,
    Usuario,
)
from app.services import checklist_responsaveis


class _ClienteIsolado(FlaskClient):
    def open(self, *args, **kwargs):
        # A fixture mantém um app_context por teste. Em produção cada request
        # ganha outro g; simular isso evita compartilhar current_user entre
        # os clientes independentes (admin e responsável) deste cenário.
        g.pop('_login_user', None)
        return super().open(*args, **kwargs)


def _cliente(app, usuario):
    cliente = _ClienteIsolado(app, app.response_class)
    with cliente.session_transaction() as sessao:
        sessao['_user_id'] = str(usuario.id)
        sessao['_fresh'] = True
    return cliente


def _pessoa(nome, loja=None, *, papel='funcionario', somente_treino=True,
            senha_provisoria=False, ativo=True, cargo='ATENDENTE'):
    usuario = Usuario(nome=nome, login=nome, papel=papel,
                      somente_treino=somente_treino,
                      senha_provisoria=senha_provisoria)
    usuario.set_senha('123')
    db.session.add(usuario)
    db.session.flush()
    pessoa = Funcionario(nome=nome, cpf=f'{usuario.id:011d}', ativo=ativo,
                         usuario=usuario, funcao=cargo, periodo='Manhã',
                         lojas=[loja] if loja else [])
    db.session.add(pessoa)
    db.session.commit()
    return pessoa


def _adicionar(cliente, pessoa, loja, periodo='Tarde'):
    resposta = cliente.post('/checklist/responsaveis/adicionar', data={
        'funcionario_id': pessoa.id, 'loja_id': loja.id, 'periodo': periodo,
    })
    assert resposta.status_code == 302
    return ChecklistResponsavel.query.filter_by(
        funcionario_id=pessoa.id, loja_id=loja.id, periodo=periodo).one()


def _turno(loja, periodo):
    quadro = checklist_responsaveis.quadro(loja.id)
    return next(linha for linha in quadro['lojas']
                if linha['loja'].id == loja.id)['turnos'][periodo]


def test_adicionar_concede_checklist_sem_modificar_cadastro_ou_perfil(
        app, admin_user, loja):
    origem = Loja(nome='Unidade de origem', ativa=True)
    db.session.add(origem)
    db.session.commit()
    pessoa = _pessoa('Pessoa vinculada', origem)
    antes = (pessoa.funcao, pessoa.cargo_id, pessoa.periodo,
             [lj.id for lj in pessoa.lojas], pessoa.usuario.papel,
             pessoa.usuario.somente_treino, pessoa.usuario.loja_id,
             pessoa.usuario.senha_hash)
    assert pessoa.usuario.pode_checklist() is False

    vinculo = _adicionar(_cliente(app, admin_user), pessoa, loja)

    assert vinculo.ativo is True
    assert vinculo.criado_por_id == admin_user.id
    assert pessoa.usuario.pode_checklist() is True
    assert antes == (pessoa.funcao, pessoa.cargo_id, pessoa.periodo,
                     [lj.id for lj in pessoa.lojas], pessoa.usuario.papel,
                     pessoa.usuario.somente_treino, pessoa.usuario.loja_id,
                     pessoa.usuario.senha_hash)
    assert [p['nome'] for p in _turno(loja, 'Tarde')] == [pessoa.nome]


def test_duplicata_nao_duplica_e_reativacao_preserva_mesmo_vinculo(
        app, admin_user, loja):
    pessoa = _pessoa('Pessoa repetida')
    cliente = _cliente(app, admin_user)
    vinculo = _adicionar(cliente, pessoa, loja)
    original_id = vinculo.id
    _adicionar(cliente, pessoa, loja)
    assert ChecklistResponsavel.query.count() == 1

    resposta = cliente.post(f'/checklist/responsaveis/{original_id}/remover')
    assert resposta.status_code == 302
    assert vinculo.ativo is False
    assert pessoa.usuario.pode_checklist() is False

    reativado = _adicionar(cliente, pessoa, loja)
    assert reativado.id == original_id
    assert reativado.ativo is True
    assert ChecklistResponsavel.query.count() == 1


@pytest.mark.parametrize('invalido', [
    'funcionario_inexistente', 'funcionario_texto', 'funcionario_inativo',
    'loja_inexistente', 'loja_texto', 'loja_inativa', 'industria',
    'periodo_invalido', 'periodo_vazio', 'observador',
])
def test_rejeita_vinculo_invalido_com_aviso_sem_gravar(
        app, admin_user, loja, invalido):
    pessoa = _pessoa('Pessoa inválida')
    dados = {'funcionario_id': pessoa.id, 'loja_id': loja.id, 'periodo': 'Manhã'}
    if invalido == 'funcionario_inexistente':
        dados['funcionario_id'] = 999999
    elif invalido == 'funcionario_texto':
        dados['funcionario_id'] = 'invalido'
    elif invalido == 'funcionario_inativo':
        pessoa.ativo = False
    elif invalido == 'loja_inexistente':
        dados['loja_id'] = 999999
    elif invalido == 'loja_texto':
        dados['loja_id'] = 'invalida'
    elif invalido == 'loja_inativa':
        loja.ativa = False
    elif invalido == 'industria':
        loja.nome = 'Industria'
    elif invalido == 'periodo_invalido':
        dados['periodo'] = 'Noite'
    elif invalido == 'periodo_vazio':
        dados['periodo'] = ''
    elif invalido == 'observador':
        pessoa.usuario.papel = 'observador'
    db.session.commit()
    cliente = _cliente(app, admin_user)

    resposta = cliente.post('/checklist/responsaveis/adicionar', data=dados)

    assert resposta.status_code == 302
    assert ChecklistResponsavel.query.count() == 0
    with cliente.session_transaction() as sessao:
        assert sessao.get('_flashes')


def test_operador_nao_pode_adicionar_nem_revogar_acessos(
        app, admin_user, loja):
    pessoa = _pessoa('Operador', somente_treino=False)
    vinculo = _adicionar(_cliente(app, admin_user), pessoa, loja)
    cliente = _cliente(app, pessoa.usuario)
    assert cliente.post('/checklist/responsaveis/adicionar', data={
        'funcionario_id': pessoa.id, 'loja_id': loja.id, 'periodo': 'Manhã',
    }).status_code == 403
    assert cliente.post(
        f'/checklist/responsaveis/{vinculo.id}/remover').status_code == 403
    assert vinculo.ativo is True
    assert ChecklistResponsavel.query.count() == 1


def test_vinculo_sem_conta_aparece_pendente_e_passara_a_valer_ao_vincular_conta(
        app, admin_user, loja):
    pessoa = Funcionario(nome='Ainda sem conta', cpf='98765432100', ativo=True,
                         funcao='ATENDENTE')
    db.session.add(pessoa)
    db.session.commit()
    _adicionar(_cliente(app, admin_user), pessoa, loja)
    entrada = _turno(loja, 'Tarde')[0]
    assert entrada['nome'] == pessoa.nome
    assert entrada['acesso'] is False
    assert pessoa.usuario_id is None

    usuario = Usuario(nome=pessoa.nome, login='novo.checklist',
                      papel='funcionario', somente_treino=True)
    usuario.set_senha('123')
    db.session.add(usuario)
    db.session.flush()
    pessoa.usuario = usuario
    db.session.commit()
    assert usuario.pode_checklist() is True
    assert _turno(loja, 'Tarde')[0]['acesso'] is True


def test_deduplica_responsavel_do_rh_e_preserva_rh_ao_remover_extra(
        app, admin_user, loja):
    pessoa = _pessoa('Chefe do RH', loja, cargo='ATENDENTE CHEFE',
                     somente_treino=False)
    cliente = _cliente(app, admin_user)
    vinculo = _adicionar(cliente, pessoa, loja, periodo='Manhã')
    assert [p['nome'] for p in _turno(loja, 'Manhã')] == [pessoa.nome]
    cliente.post(f'/checklist/responsaveis/{vinculo.id}/remover')
    assert [p['nome'] for p in _turno(loja, 'Manhã')] == [pessoa.nome]
    assert pessoa.funcao == 'ATENDENTE CHEFE'
    assert pessoa.periodo == 'Manhã'
    assert [lj.id for lj in pessoa.lojas] == [loja.id]


def test_restrito_preenche_checklist_em_outra_loja_sem_ganhar_outras_telas(
        app, admin_user, loja):
    outra = Loja(nome='Cobertura em outra loja', ativa=True)
    item = ChecklistItemModelo(tipo='abertura', texto='Conferir vitrine', ativo=True)
    db.session.add_all([outra, item])
    db.session.commit()
    pessoa = _pessoa('Cobertura', loja)
    _adicionar(_cliente(app, admin_user), pessoa, loja)
    cliente = _cliente(app, pessoa.usuario)
    app.config['UI_V2_ENABLED'] = True

    resposta = cliente.get(f'/checklist/?loja={outra.id}')
    assert resposta.status_code == 200
    assert 'href="/checklist/"' in resposta.get_data(as_text=True)
    url = f'/checklist/preencher?loja={outra.id}&tipo=abertura'
    assert cliente.get(url).status_code == 200
    assert cliente.post(url, data={f'ok_{item.id}': 'ok'}).status_code == 302
    preenchimento = ChecklistPreenchimento.query.one()
    assert preenchimento.loja_id == outra.id
    assert preenchimento.usuario_id == pessoa.usuario_id
    for protegido in ('/checklist/config', '/checklist/responsaveis',
                       '/checklist/conferencia', '/pedidos/', '/producao/'):
        negada = cliente.get(protegido)
        assert negada.status_code == 302
        assert '/treino' in negada.location
    assert pessoa.usuario.papel == 'funcionario'
    assert pessoa.usuario.somente_treino is True


def test_revogacao_bloqueia_get_e_post_sem_apagar_autoria_passada(
        app, admin_user, loja):
    pessoa = _pessoa('Revogado')
    item = ChecklistItemModelo(tipo='abertura', texto='Portas', ativo=True)
    db.session.add(item)
    db.session.commit()
    admin = _cliente(app, admin_user)
    vinculo = _adicionar(admin, pessoa, loja)
    cliente = _cliente(app, pessoa.usuario)
    url = f'/checklist/preencher?loja={loja.id}&tipo=abertura'
    assert cliente.post(url, data={f'ok_{item.id}': 'ok'}).status_code == 302
    preenchimento = ChecklistPreenchimento.query.one()
    admin.post(f'/checklist/responsaveis/{vinculo.id}/remover')

    for resposta in (cliente.get('/checklist/'), cliente.get(url),
                     cliente.post(url, data={f'ok_{item.id}': 'ok'})):
        assert resposta.status_code == 302
        assert '/treino' in resposta.location
    assert ChecklistPreenchimento.query.count() == 1
    assert preenchimento.usuario_id == pessoa.usuario_id


@pytest.mark.parametrize('inativar', ['funcionario', 'loja', 'industria'])
def test_vinculo_perde_acesso_quando_cadastro_deixa_de_ser_operacional(
        app, admin_user, loja, inativar):
    pessoa = _pessoa('Acesso suspenso')
    _adicionar(_cliente(app, admin_user), pessoa, loja)
    assert pessoa.usuario.pode_checklist() is True
    if inativar == 'funcionario':
        pessoa.ativo = False
    elif inativar == 'loja':
        loja.ativa = False
    else:
        loja.nome = 'Industria'
    db.session.commit()
    assert pessoa.usuario.pode_checklist() is False
    resposta = _cliente(app, pessoa.usuario).get('/checklist/')
    assert resposta.status_code == 302
    assert '/treino' in resposta.location


def test_senha_provisoria_continua_exigindo_troca_mesmo_com_vinculo(
        app, admin_user, loja):
    pessoa = _pessoa('Primeiro acesso', senha_provisoria=True)
    _adicionar(_cliente(app, admin_user), pessoa, loja)
    cliente = _cliente(app, pessoa.usuario)
    for resposta in (cliente.get('/checklist/'), cliente.post(
            f'/checklist/preencher?loja={loja.id}&tipo=abertura')):
        assert resposta.status_code == 302
        assert '/minha-senha' in resposta.location
    assert ChecklistPreenchimento.query.count() == 0


def test_vinculo_amplia_so_checklist_para_papel_sem_capacidade_e_observador_nunca(
        app, admin_user, loja):
    pessoa = _pessoa('Padeiro de cobertura', papel='padeiro', somente_treino=False)
    assert pessoa.usuario.pode_checklist() is False
    _adicionar(_cliente(app, admin_user), pessoa, loja)
    assert pessoa.usuario.pode_checklist() is True
    assert _cliente(app, pessoa.usuario).get('/checklist/').status_code == 200

    pessoa.usuario.papel = 'observador'
    db.session.commit()
    assert pessoa.usuario.pode_checklist() is False
    cliente = _cliente(app, pessoa.usuario)
    assert cliente.get('/checklist/').status_code != 200
    assert cliente.post('/checklist/preencher').status_code == 403


def test_papel_original_mantem_acesso_sem_vinculo(app, loja):
    pessoa = _pessoa('Funcionário comum', loja, somente_treino=False)
    assert pessoa.usuario.pode_checklist() is True
    assert _cliente(app, pessoa.usuario).get('/checklist/').status_code == 200


def test_concessao_e_revogacao_deixam_auditoria_do_admin(app, admin_user, loja):
    pessoa = _pessoa('Auditada')
    cliente = _cliente(app, admin_user)
    vinculo = _adicionar(cliente, pessoa, loja)
    cliente.post(f'/checklist/responsaveis/{vinculo.id}/remover')
    logs = AuditLog.query.filter_by(
        tabela=ChecklistResponsavel.__tablename__, registro_id=vinculo.id
    ).order_by(AuditLog.id).all()
    assert [log.acao for log in logs] == ['insert', 'update']
    assert all(log.usuario_id == admin_user.id for log in logs)
    assert json.loads(logs[0].depois)['ativo'] is True
    assert json.loads(logs[-1].antes)['ativo'] is True
    assert json.loads(logs[-1].depois)['ativo'] is False


@pytest.mark.parametrize('ativo', [True, False])
def test_excluir_loja_preserva_historico_de_responsavel(app, owner_user, loja, ativo):
    pessoa = _pessoa('Pessoa sem vínculo RH nesta loja')
    cliente = _cliente(app, owner_user)
    vinculo = _adicionar(cliente, pessoa, loja)
    vinculo.ativo = ativo
    db.session.commit()
    resposta = cliente.post(f'/rh/lojas/excluir/{loja.id}')
    assert resposta.status_code == 302
    assert db.session.get(Loja, loja.id) is not None
    assert db.session.get(ChecklistResponsavel, vinculo.id) is not None
    with cliente.session_transaction() as sessao:
        assert any('histórico de responsáveis' in texto for _, texto in sessao['_flashes'])


@pytest.mark.parametrize('dono', [True, False])
def test_atalhos_de_conta_respeitam_permissao_rh(app, admin_user, owner_user, loja, dono):
    pessoa = _pessoa('Pessoa com conta')
    sem_conta = Funcionario(nome='Pessoa sem conta', cpf='00000007891', ativo=True)
    db.session.add(sem_conta)
    db.session.commit()
    cliente = _cliente(app, owner_user if dono else admin_user)
    _adicionar(cliente, pessoa, loja)
    _adicionar(cliente, sem_conta, loja)
    html = cliente.get('/checklist/responsaveis').get_data(as_text=True)
    assert ('>Ver conta e e-mail</a>' in html) is dono
    assert ('>Criar ou vincular acesso</a>' in html) is dono
    assert ('Peça ao dono' in html) is not dono

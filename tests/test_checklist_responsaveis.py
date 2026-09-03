"""Responsáveis seguem vínculos do RH sem promover contas ou reescrever autoria."""
import pytest

from app.extensions import db
from app.models import ChecklistItemModelo, ChecklistPreenchimento, Funcionario, Loja, Usuario
from app.models.rh import funcionario_loja
from app.services import checklist_responsaveis


@pytest.fixture
def client(app):
    return app.test_client()


def _pessoa(nome, loja=None, periodo='Manhã', cargo='ATENDENTE CHEFE',
            papel='funcionario', ativo=True, somente_treino=False):
    usuario = Usuario(nome=nome, login=nome, papel=papel,
                      somente_treino=somente_treino)
    usuario.set_senha('123')
    db.session.add(usuario)
    db.session.flush()
    funcionario = Funcionario(nome=nome, cpf=f'{usuario.id:011d}',
                              funcao=cargo, usuario=usuario, periodo=periodo,
                              ativo=ativo, lojas=[loja] if loja else [])
    db.session.add(funcionario)
    db.session.commit()
    return funcionario


def _nomes(quadro, loja_id, periodo):
    linha = next(linha for linha in quadro['lojas'] if linha['loja'].id == loja_id)
    return [p['nome'] for p in linha['turnos'][periodo]]


def test_vincula_chefes_e_perfil_gerente_sem_incluir_gerencia_rh(app, loja):
    _pessoa('Chefe', loja)
    _pessoa('Gerente', loja, cargo='GERENTE', periodo='Tarde')
    _pessoa('LiderOperacional', loja, cargo='ATENDENTE 2', papel='gerente', periodo='Tarde')
    _pessoa('RH', loja, cargo='GERENTE DE RH', papel='admin')
    _pessoa('Geral', loja, cargo='GERENTE GERAL', papel='admin')
    _pessoa('Atendente', loja, cargo='ATENDENTE')
    _pessoa('Desligado', loja, ativo=False)
    quadro = checklist_responsaveis.quadro()
    assert _nomes(quadro, loja.id, 'Manhã') == ['Chefe']
    assert _nomes(quadro, loja.id, 'Tarde') == ['Gerente', 'LiderOperacional']


def test_mudanca_de_unidade_e_periodo_atualiza_vinculo_sem_copia(app, loja):
    outra = Loja(nome='Outra', ativa=True)
    db.session.add(outra)
    db.session.commit()
    pessoa = _pessoa('Chefe', loja)
    pessoa.lojas.append(outra)
    db.session.flush()
    db.session.execute(funcionario_loja.update().where(
        funcionario_loja.c.funcionario_id == pessoa.id,
        funcionario_loja.c.loja_id == outra.id).values(loja_principal=True))
    pessoa.periodo = 'Tarde'
    db.session.commit()
    quadro = checklist_responsaveis.quadro()
    assert _nomes(quadro, loja.id, 'Manhã') == []
    assert _nomes(quadro, outra.id, 'Tarde') == ['Chefe']
    assert checklist_responsaveis.loja_do_usuario(pessoa.usuario) == outra.id
    assert pessoa.usuario.loja_id is None


def test_unidade_ambigua_e_periodo_ausente_ficam_pendentes(app, loja):
    outra = Loja(nome='Outra', ativa=True)
    db.session.add(outra)
    db.session.commit()
    pessoa = _pessoa('DuasUnidades', loja)
    pessoa.lojas.append(outra)
    _pessoa('SemPeriodo', loja, periodo=None)
    db.session.commit()
    quadro = checklist_responsaveis.quadro()
    assert _nomes(quadro, loja.id, 'Manhã') == []
    assert {p['nome'] for p in quadro['pendentes']} == {'DuasUnidades', 'SemPeriodo'}


def test_acesso_pendente_nao_remove_responsavel_nem_muda_permissoes(app, loja):
    pessoa = _pessoa('SoTreino', loja, somente_treino=True)
    sem_login = Funcionario(nome='SemLogin', cpf='99999999999', ativo=True,
                            funcao='ATENDENTE CHEFE', periodo='Tarde', lojas=[loja])
    db.session.add(sem_login)
    db.session.commit()
    quadro = checklist_responsaveis.quadro(loja.id)
    assert quadro['lojas'][0]['turnos']['Manhã'][0]['acesso'] is False
    assert quadro['lojas'][0]['turnos']['Tarde'][0]['acesso'] is False
    assert pessoa.usuario.somente_treino is True
    assert pessoa.usuario.papel == 'funcionario'
    assert sem_login.usuario_id is None


def test_loja_sem_responsavel_aparece_e_industria_nao(app, loja):
    industria = Loja(nome='Industria', ativa=True)
    db.session.add(industria)
    db.session.commit()
    _pessoa('Industria', industria, cargo='GERENTE')
    quadro = checklist_responsaveis.quadro()
    assert [linha['loja'].id for linha in quadro['lojas']] == [loja.id]
    assert quadro['lojas'][0]['turnos'] == {'Manhã': [], 'Tarde': []}


def test_hub_usa_unidade_do_rh_e_mantem_escolha_explicita(app, client, loja):
    outra = Loja(nome='Outra', ativa=True)
    db.session.add(outra)
    db.session.commit()
    pessoa = _pessoa('Chefe', loja)
    pessoa.usuario.loja_id = outra.id  # vínculo legado divergente
    db.session.commit()
    client.post('/auth/login', data={'login': 'Chefe', 'senha': '123'})
    html = client.get('/checklist/').get_data(as_text=True)
    assert f'value="{loja.id}" selected' in html
    assert 'Chefe' in html
    html = client.get(f'/checklist/?loja={outra.id}').get_data(as_text=True)
    assert f'value="{outra.id}" selected' in html
    assert 'Sem responsável vinculado.' in html
    assert pessoa.usuario.loja_id == outra.id


def test_quadro_admin_e_bloqueio_so_treino(app, client, loja, admin_user):
    _pessoa('Chefe', loja)
    client.post('/auth/login', data={'login': 'Chefe', 'senha': '123'})
    assert client.get('/checklist/responsaveis').status_code == 403
    client.get('/auth/logout')
    client.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    resposta = client.get('/checklist/responsaveis')
    assert resposta.status_code == 200
    assert 'Chefe' in resposta.get_data(as_text=True)
    client.get('/auth/logout')
    _pessoa('Treino', loja, somente_treino=True)
    client.post('/auth/login', data={'login': 'Treino', 'senha': '123'})
    resposta = client.get('/checklist/')
    assert resposta.status_code == 302
    assert '/treino' in resposta.location


def test_substituto_pode_preencher_e_autoria_real_e_preservada(app, client, loja):
    _pessoa('Chefe', loja)
    substituto = _pessoa('Substituto', loja, cargo='ATENDENTE')
    item = ChecklistItemModelo(tipo='abertura', texto='Vitrine', ativo=True)
    db.session.add(item)
    db.session.commit()
    client.post('/auth/login', data={'login': 'Substituto', 'senha': '123'})
    url = f'/checklist/preencher?loja={loja.id}&tipo=abertura'
    assert client.get(url).status_code == 200
    erro = client.post(url, data={})
    assert erro.status_code == 422
    assert 'Responsáveis por período' in erro.get_data(as_text=True)
    resposta = client.post(url, data={f'ok_{item.id}': 'ok'})
    assert resposta.status_code == 302
    assert ChecklistPreenchimento.query.one().usuario_id == substituto.usuario_id

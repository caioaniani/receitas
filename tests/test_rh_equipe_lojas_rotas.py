"""A visão por loja é consultiva, restrita ao dono e navegável sem JavaScript."""
import pytest
from flask import g

from app.extensions import db
from app.models import Cargo, Funcionario, Loja, Usuario


def _cliente(app, usuario=None):
    g.pop('_login_user', None)
    cliente = app.test_client()
    if usuario:
        with cliente.session_transaction() as sessao:
            sessao['_user_id'] = str(usuario.id)
            sessao['_fresh'] = True
    return cliente


def _equipe():
    centro = Loja(nome='Loja Centro visual', ativa=True)
    jardim = Loja(nome='Loja Jardim visual', ativa=True)
    cargo = Cargo(nome='Atendente', salario_base=9287.61)
    lider = Funcionario(nome='Líder visual', cpf='81122233341', ativo=True,
                        lojas=[centro], periodo='Manhã')
    ana = Funcionario(nome='Ana visual', cpf='81122233342', ativo=True,
                      lojas=[centro], periodo='Manhã', lider=lider, cargo=cargo,
                      email='sigilo-ana@example.test', salario_base=8297.61)
    bia = Funcionario(nome='Bia visual', cpf='81122233343', ativo=True,
                      lojas=[jardim], periodo='Tarde', cargo=cargo)
    db.session.add_all([centro, jardim, cargo, lider, ana, bia])
    db.session.commit()
    return centro, jardim, lider, ana, bia


def test_owner_ve_lojas_lideres_e_pessoas_sem_login(app, owner_user):
    centro, jardim, lider, ana, bia = _equipe()
    cliente = _cliente(app, owner_user)
    resposta = cliente.get('/rh/equipe/lojas')
    html = resposta.get_data(as_text=True)

    assert resposta.status_code == 200
    for texto in (centro.nome, jardim.nome, lider.nome, ana.nome, bia.nome,
                  'Equipe por loja', 'Líder direto', 'Equipe liderada',
                  'Sem líder direto cadastrado', 'Manhã', 'Tarde'):
        assert texto in html
    assert ana.usuario_id is None
    assert 'href="/rh/equipe/lojas" aria-current="page"' in html
    assert 'aria-label="Treinamento e pessoas"' in html
    assert f'href="/rh/funcionarios/{ana.id}"' in html
    for privado in (ana.cpf, ana.email, '8297.61', '8.297,61', '9287.61', '9.287,61'):
        assert privado not in html


def test_selecao_nao_mistura_pessoas_de_outras_lojas(app, owner_user):
    centro, jardim, lider, ana, bia = _equipe()
    html = _cliente(app, owner_user).get(
        '/rh/equipe/lojas', query_string={'loja': centro.id}).get_data(as_text=True)
    assert ana.nome in html and lider.nome in html
    assert bia.nome not in html
    assert jardim.nome in html  # seletor permite trocar a unidade
    assert 'Ver só esta loja' not in html


@pytest.mark.parametrize('loja_id', ['inexistente', '99999', '-1', '1 OR 1=1'])
def test_loja_invalida_nao_cai_silenciosamente_em_todas(app, owner_user, loja_id):
    assert _cliente(app, owner_user).get(
        '/rh/equipe/lojas', query_string={'loja': loja_id}).status_code == 404


def test_visitante_precisa_login_e_admin_comum_nao_tem_acesso(app, admin_user):
    visitante = _cliente(app).get('/rh/equipe/lojas')
    assert visitante.status_code == 302
    assert '/auth/login' in visitante.location
    assert _cliente(app, admin_user).get('/rh/equipe/lojas').status_code == 403


@pytest.mark.parametrize('papel,treino,login', [
    ('gerente', False, 'gerente-visual'),
    ('funcionario', False, 'funcionario-visual'),
    ('funcionario', True, 'aluno-visual'),
    ('admin', True, 'dakson'),
])
def test_nova_visao_nao_amplia_permissoes(app, papel, treino, login):
    usuario = Usuario(nome='Usuário restrito', login=login, papel=papel,
                      somente_treino=treino)
    usuario.set_senha('somente-teste')
    db.session.add(usuario)
    db.session.commit()
    resposta = _cliente(app, usuario).get('/rh/equipe/lojas')
    assert resposta.status_code in (302, 403)
    if resposta.status_code == 302:
        assert resposta.location.endswith('/treino/')
    assert 'Equipe por loja' not in resposta.get_data(as_text=True)


def test_vazia_tem_orientacao_sem_erro(app, owner_user):
    html = _cliente(app, owner_user).get('/rh/equipe/lojas').get_data(as_text=True)
    assert 'Nenhuma loja ativa cadastrada' in html


@pytest.mark.parametrize('caminho', ['/rh/equipe', '/rh/funcionarios'])
def test_atalhos_no_rh(app, owner_user, caminho):
    html = _cliente(app, owner_user).get(caminho).get_data(as_text=True)
    assert 'href="/rh/equipe/lojas">Por loja</a>' in html
    assert '>Equipe por loja</a>' in html


def test_nomes_escapados_e_consulta_preserva_cadastro(app, owner_user):
    centro, _, lider, ana, _ = _equipe()
    ana.nome = '<script>alert("nome")</script>'
    centro.nome = '<img src=x onerror=alert(1)>'
    db.session.commit()
    antes = (ana.lider_id, ana.periodo, [loja.id for loja in ana.lojas],
             Funcionario.query.count(), Usuario.query.count())
    html = _cliente(app, owner_user).get('/rh/equipe/lojas').get_data(as_text=True)
    assert '<script>alert("nome")</script>' not in html
    assert '<img src=x onerror=alert(1)>' not in html
    assert '&lt;script&gt;' in html and '&lt;img src=x' in html
    db.session.expire_all()
    assert antes == (ana.lider_id, ana.periodo, [loja.id for loja in ana.lojas],
                    Funcionario.query.count(), Usuario.query.count())
    assert ana.lider_id == lider.id


def test_lider_de_lider_preserva_superior_sem_falso_vazio(app, owner_user):
    centro, _, lider, ana, _ = _equipe()
    superior = Funcionario(nome='Superior visual', cpf='81122233344', ativo=True,
                           lojas=[centro], periodo='Manhã')
    lider.lider = superior
    db.session.add(superior)
    db.session.commit()
    html = _cliente(app, owner_user).get('/rh/equipe/lojas').get_data(as_text=True)
    assert 'Responde a Superior visual' in html
    assert 'Sem liderados diretos neste período' not in html
    assert lider.nome in html and ana.nome in html


def test_lider_com_superior_inativo_mostra_alerta_no_cabecalho(app, owner_user):
    _, _, lider, _, _ = _equipe()
    superior = Funcionario(nome='Superior inativo', cpf='81122233345', ativo=False)
    db.session.add(superior)
    lider.lider = superior
    db.session.commit()
    html = _cliente(app, owner_user).get('/rh/equipe/lojas').get_data(as_text=True)
    assert 'Líder cadastrado está inativo.' in html

import pytest

from app.extensions import db
from app.models import Cargo, Funcionario, Usuario
from tests.test_rh_pessoas_interface import _cliente


def _cargo(ativo=True):
    cargo = Cargo(nome='Função de teste', salario_base=2345.67, ativo=ativo)
    db.session.add(cargo)
    db.session.commit()
    return cargo


def test_novo_funcionario_escolhe_funcao_e_salario_oficial(app, owner_user):
    cargo = _cargo()
    cliente = _cliente(app, owner_user)
    html = cliente.get('/rh/funcionarios/novo').get_data(as_text=True)
    assert 'name="cargo_id"' in html
    assert 'Função de teste — R$ 2.345,67' in html
    assert 'name="funcao"' not in html
    assert 'name="salario_base"' not in html
    resposta = cliente.post('/rh/funcionarios/novo', data={
        'nome': 'Pessoa teste', 'cpf': '82000000111', 'cargo_id': cargo.id,
        'salario_base': '9999', 'funcao': 'Cargo adulterado',
    })
    assert resposta.status_code == 302
    pessoa = Funcionario.query.filter_by(cpf='82000000111').one()
    assert pessoa.cargo_id == cargo.id
    assert pessoa.funcao == cargo.nome
    assert pessoa.salario_base == pessoa.salario_efetivo() == cargo.salario_base


@pytest.mark.parametrize('valor', ['', 'abc', '999999', 'inativo'])
def test_cargo_invalido_nao_cadastra_funcionario(app, owner_user, valor):
    inativo = _cargo(ativo=False)
    cliente = _cliente(app, owner_user)
    html = cliente.get('/rh/funcionarios/novo').get_data(as_text=True)
    assert 'Função de teste' not in html
    resposta = cliente.post('/rh/funcionarios/novo', data={
        'nome': 'Pessoa teste', 'cpf': '82000000111',
        'cargo_id': inativo.id if valor == 'inativo' else valor,
    })
    assert resposta.status_code == 400
    assert Funcionario.query.filter_by(cpf='82000000111').first() is None


def test_lista_de_funcoes_nao_expoe_salario_para_dakson(app):
    _cargo()
    usuario = Usuario(nome='Dakson teste', login='dakson', papel='admin', somente_treino=True)
    usuario.set_senha('somente-fixture')
    db.session.add(usuario)
    db.session.flush()
    db.session.add(Funcionario(nome='Dakson teste', cpf='82000000112', usuario_id=usuario.id, ativo=True))
    db.session.commit()
    resposta = _cliente(app, usuario).get('/rh/funcionarios/novo')
    assert resposta.status_code == 200
    html = resposta.get_data(as_text=True)
    assert 'Função de teste' in html
    assert 'data-salario' not in html
    assert '2.345,67' not in html and '2345.67' not in html
    assert 'Gerenciar funções e salários' not in html

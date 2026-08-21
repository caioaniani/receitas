"""Vínculo automático e conservador entre função legada e Cargo."""
from app.extensions import db
from app.models import Cargo, Funcionario
from app.services import rh_cargos


def _funcionario(nome, cpf, funcao, cargo=None):
    funcionario = Funcionario(
        nome=nome, cpf=cpf, funcao=funcao,
        cargo_id=cargo.id if cargo else None,
    )
    db.session.add(funcionario)
    db.session.commit()
    return funcionario


def _owner(app, owner_user):
    cliente = app.test_client()
    with cliente.session_transaction() as sessao:
        sessao['_user_id'] = str(owner_user.id)
        sessao['_fresh'] = True
    return cliente


def test_associa_por_nome_ignorando_caixa_acentos_e_espacos(app):
    with app.app_context():
        cargo = Cargo(nome='Auxiliar de Produção', salario_base=0)
        db.session.add(cargo)
        db.session.commit()
        funcionario = _funcionario(
            'Ana', '82000000001', '  AUXILIAR   DE PRODUCAO ')

        resultado = rh_cargos.associar_pendentes(commit=True)

        assert resultado['associados'] == [(funcionario, cargo)]
        assert db.session.get(Funcionario, funcionario.id).cargo_id == cargo.id


def test_nao_chuta_nome_diferente_nem_substitui_cargo_existente(app):
    with app.app_context():
        atendente = Cargo(nome='Atendente', salario_base=0)
        padeiro = Cargo(nome='Padeiro', salario_base=0)
        db.session.add_all([atendente, padeiro])
        db.session.commit()
        duvidoso = _funcionario(
            'Bia', '82000000002', 'Atendente chefe de turno')
        definido = _funcionario(
            'Caio', '82000000003', 'Atendente', cargo=padeiro)

        resultado = rh_cargos.associar_pendentes(
            [duvidoso, definido], commit=True)

        assert duvidoso in resultado['sem_correspondencia']
        assert duvidoso.cargo_id is None
        assert definido.cargo_id == padeiro.id


def test_backfill_deploy_liga_funcionario_existente(app):
    with app.app_context():
        cargo = Cargo(nome='ATENDENTE', salario_base=0)
        db.session.add(cargo)
        db.session.commit()
        funcionario = _funcionario(
            'Dani', '82000000004', 'Atendente')

        from app.migrations_legacy import _backfill_cargos_funcionarios
        _backfill_cargos_funcionarios(app)

        assert db.session.get(Funcionario, funcionario.id).cargo_id == cargo.id


def test_progressao_explica_cargo_ausente_e_cargo_sem_trilha(
        app, owner_user):
    with app.app_context():
        cargo = Cargo(nome='Atendimento', salario_base=0)
        db.session.add(cargo)
        db.session.commit()
        _funcionario('Eva', '82000000005', None)
        _funcionario('Fabi', '82000000006', 'Atendimento', cargo=cargo)

    html = _owner(app, owner_user).get(
        '/treino/gestor/progressao').get_data(as_text=True)

    assert 'Cargo ainda não vinculado no RH' in html
    assert 'Vincular cargo' in html
    assert 'Nenhuma trilha obrigatória para este cargo' in html

"""Vínculo automático e consolidação explícita e conservadora entre cargos."""
import json

import pytest

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


def test_equivalencia_explicita_apenas_atendente_nivel_1(app):
    assert rh_cargos.nome_cargo_exibicao('  ATENDENTE ') == 'Atendente 1'
    assert rh_cargos.nome_cargo_exibicao('atendente   1') == 'Atendente 1'
    assert rh_cargos.nome_cargo_exibicao('Atendente 2') == 'Atendente 2'
    assert rh_cargos.nome_cargo_exibicao('ATENDENTE CHEFE') == 'ATENDENTE CHEFE'
    assert rh_cargos.nome_cargo_exibicao('Atendente 10') == 'Atendente 10'
    legado = Cargo(nome='ATENDENTE', salario_base=2330.4)
    plano = Cargo(nome='Atendente 1', salario_base=2130.4)
    db.session.add_all([legado, plano])
    db.session.commit()
    assert rh_cargos.encontrar_cargo_equivalente('Atendente 1') is None
    plano.ativo = False
    db.session.commit()
    assert rh_cargos.encontrar_cargo_equivalente('Atendente 1') == legado
    assert rh_cargos.encontrar_cargo_equivalente('Atendente 2') is None


def test_unificacao_preserva_cargo_populado_com_salario_diferente_da_proposta(
        app, owner_user):
    from app.models import (
        AuditLog,
        PlanoCarreiraCargoVinculo,
        PlanoCarreiraFaixa,
        PlanoCarreiraImportacao,
    )

    legado = Cargo(nome='ATENDENTE', salario_base=2330.4)
    plano = Cargo(nome='Atendente 1', salario_base=2130.4)
    nivel2 = Cargo(nome='Atendente 2', salario_base=2330.4)
    importacao = PlanoCarreiraImportacao(nome_arquivo='plano.xlsx', sha256='a' * 64)
    db.session.add_all([legado, plano, nivel2, importacao])
    db.session.commit()
    funcionario = _funcionario('Maria', '82000000011', 'ATENDENTE', legado)
    funcionario.salario_base = 2100
    faixa = PlanoCarreiraFaixa(
        importacao_id=importacao.id, familia='Atendimento', nivel=1,
        cargo_proposto='Atendente 1', salario_nivel=2130.4)
    db.session.add(faixa)
    db.session.flush()
    vinculo = PlanoCarreiraCargoVinculo(faixa_id=faixa.id, cargo_id=plano.id)
    db.session.add(vinculo)
    db.session.commit()

    previa = rh_cargos.resumo_unificacao_atendentes()
    assert previa['alvo'] == legado
    assert previa['aplicavel'] is True
    assert previa['impedimentos'] == []
    assert plano.ativo is True
    assert vinculo.cargo_id == plano.id

    rh_cargos.unificar_atendentes(owner_user.id, commit=True)

    assert funcionario.cargo_id == legado.id
    assert funcionario.salario_efetivo() == 2330.4
    assert funcionario.salario_base == 2100
    assert legado.salario_base == 2330.4
    assert plano.salario_base == 2130.4 and plano.ativo is False
    assert nivel2.ativo is True
    assert vinculo.cargo_id == legado.id
    assert faixa.salario_nivel == 2130.4  # proposta não é remuneração contratual
    assert Cargo.query.count() == 3  # nenhum cargo/histórico apagado
    audit = AuditLog.query.filter_by(tabela='rh_cargo_unificacao').one()
    assert audit.usuario_id == owner_user.id
    assert json.loads(audit.antes)['vinculos_plano'][0]['cargo_id'] == plano.id
    assert json.loads(audit.depois)['remuneracao_alterada'] is False

    repeticao = rh_cargos.unificar_atendentes(owner_user.id, commit=True)
    assert repeticao['ja_unificado'] is True
    assert AuditLog.query.filter_by(tabela='rh_cargo_unificacao').count() == 1


def test_unificacao_remapeia_pessoas_e_preserva_uniao_das_trilhas(
        app, owner_user):
    from app.models import TreinoTrilha, TreinoTrilhaCargo

    legado = Cargo(nome='ATENDENTE', salario_base=2330.4)
    origem = Cargo(nome='Atendente 1', salario_base=2330.4)
    cultura = TreinoTrilha(nome='Cultura')
    atendimento = TreinoTrilha(nome='Atendimento')
    db.session.add_all([legado, origem, cultura, atendimento])
    db.session.commit()
    _funcionario('A', '82000000012', 'ATENDENTE', legado)
    pessoa = _funcionario('B', '82000000013', 'Atendente 1', origem)
    pessoa.salario_base = 1999  # o cache não deve ser reescrito
    destino_cultura = TreinoTrilhaCargo(
        trilha_id=cultura.id, cargo_id=legado.id, obrigatoria=False)
    fonte_cultura = TreinoTrilhaCargo(
        trilha_id=cultura.id, cargo_id=origem.id, obrigatoria=True)
    fonte_atendimento = TreinoTrilhaCargo(
        trilha_id=atendimento.id, cargo_id=origem.id, obrigatoria=True)
    db.session.add_all([destino_cultura, fonte_cultura, fonte_atendimento])
    db.session.commit()

    rh_cargos.unificar_atendentes(owner_user.id, commit=True)

    assert pessoa.cargo_id == legado.id and pessoa.cargo == legado
    assert pessoa.funcao == legado.nome
    assert pessoa.salario_base == 1999
    assert pessoa.salario_efetivo() == 2330.4
    assert destino_cultura.obrigatoria is True
    assert TreinoTrilhaCargo.query.filter_by(cargo_id=legado.id).count() == 2
    assert TreinoTrilhaCargo.query.filter_by(cargo_id=origem.id).count() == 2
    assert fonte_cultura.cargo_id == origem.id
    assert fonte_atendimento.cargo_id == origem.id


@pytest.mark.parametrize('ativo', [True, False])
def test_unificacao_recusa_alteracao_salarial_inclusive_funcionario_inativo(
        app, owner_user, ativo):
    from app.models import AuditLog

    legado = Cargo(nome='ATENDENTE', salario_base=2330.4)
    origem = Cargo(nome='Atendente 1', salario_base=2130.4)
    db.session.add_all([legado, origem])
    db.session.commit()
    _funcionario('A', '82000000014', 'ATENDENTE', legado)
    pessoa = _funcionario('B', '82000000015', 'Atendente 1', origem)
    pessoa.ativo = ativo
    db.session.commit()

    previa = rh_cargos.resumo_unificacao_atendentes()
    assert previa['aplicavel'] is False
    assert 'salário efetivo' in previa['impedimentos'][0]
    with pytest.raises(ValueError, match='salário efetivo'):
        rh_cargos.unificar_atendentes(owner_user.id, commit=True)

    assert pessoa.cargo_id == origem.id
    assert pessoa.salario_efetivo() == 2130.4
    assert origem.ativo is True
    assert AuditLog.query.filter_by(tabela='rh_cargo_unificacao').count() == 0


def test_unificacao_nao_faz_commit_implicitamente_e_exige_autor(app, owner_user):
    legado = Cargo(nome='ATENDENTE', salario_base=2330.4)
    origem = Cargo(nome='Atendente 1', salario_base=2130.4)
    db.session.add_all([legado, origem])
    db.session.commit()
    with pytest.raises(ValueError, match='responsável'):
        rh_cargos.unificar_atendentes(None)

    rh_cargos.unificar_atendentes(owner_user.id)
    assert origem.ativo is False
    db.session.rollback()
    assert origem.ativo is True


def test_cadastro_e_backfill_nao_reutilizam_duplicado_inativo_apos_unificacao(
        app, owner_user):
    legado = Cargo(nome='ATENDENTE', salario_base=2330.4)
    duplicado = Cargo(nome='Atendente 1', salario_base=2130.4)
    db.session.add_all([legado, duplicado])
    db.session.commit()
    rh_cargos.unificar_atendentes(owner_user.id, commit=True)
    assert not duplicado.ativo

    # O cadastro legado usa associar_funcionario antes do commit. Um alias
    # não pode reativar o cargo descartado nem mudar a remuneração informada.
    resposta = _owner(app, owner_user).post('/rh/funcionarios/novo', data={
        'nome': 'Nova ficha', 'cpf': '82000000016',
        'funcao': 'Atendente 1', 'salario_base': '2190',
    })
    assert resposta.status_code == 302
    nova = Funcionario.query.filter_by(cpf='82000000016').one()
    assert nova.cargo_id is None and nova.salario_efetivo() == 2190

    pendente = _funcionario('Ficha antiga', '82000000017', 'Atendente 1')
    pendente.salario_base = 2230
    db.session.commit()
    resultado = rh_cargos.associar_pendentes(commit=True)
    assert nova in resultado['sem_correspondencia']
    assert pendente in resultado['sem_correspondencia']
    assert not resultado['associados']
    assert pendente.cargo_id is None and pendente.salario_efetivo() == 2230

    from app.migrations_legacy import _backfill_cargos_funcionarios
    _backfill_cargos_funcionarios(app)
    assert nova.cargo_id is None and pendente.cargo_id is None
    assert rh_cargos.encontrar_cargo('Atendente 1', [legado, duplicado]) is None
    assert rh_cargos.encontrar_cargo('ATENDENTE') == legado
    # Importação explícita tem seu próprio lookup; continua encontrando o
    # canonical ativo sem mudar a associação automática acima.
    assert rh_cargos.encontrar_cargo_equivalente('Atendente 1') == legado


def test_indice_ignora_inativo_sem_bloquear_homonimo_ativo(app):
    ativo = Cargo(nome='PADEIRO', salario_base=2500)
    inativo = Cargo(nome='Padeiro', salario_base=2000, ativo=False)
    db.session.add_all([ativo, inativo])
    db.session.commit()
    assert rh_cargos.encontrar_cargo('padeiro') == ativo

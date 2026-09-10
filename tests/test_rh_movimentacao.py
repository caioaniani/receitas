"""Histórico de cargos não inventa promoção/data e sobrevive ao novo plano."""
from datetime import date, timedelta

import pytest

from app.extensions import db
from app.models import (
    Cargo,
    Funcionario,
    PlanoCarreiraCargoVinculo,
    PlanoCarreiraEnquadramento,
    PlanoCarreiraFaixa,
    PlanoCarreiraImportacao,
    RhMovimentacao,
)
from app.services import rh_movimentacao as svc
from app.utils import agora


def _funcionario(nome_cargo='Atendente'):
    cargo = Cargo(nome=nome_cargo, salario_base=2200)
    f = Funcionario(nome='Pessoa do teste', cpf='42326669827', cargo=cargo,
                    funcao=nome_cargo, salario_base=2200)
    db.session.add_all([cargo, f])
    db.session.commit()
    return f


def _faixa(cargo, familia='Atendimento', nivel=2):
    imp = PlanoCarreiraImportacao(nome_arquivo='plano.xlsx', sha256='a' * 64)
    db.session.add(imp)
    db.session.flush()
    faixa = PlanoCarreiraFaixa(
        importacao_id=imp.id, familia=familia, nivel=nivel,
        cargo_proposto=cargo.nome)
    db.session.add(faixa)
    db.session.flush()
    db.session.add(PlanoCarreiraCargoVinculo(faixa_id=faixa.id, cargo_id=cargo.id))
    db.session.commit()
    return faixa


def test_snapshot_atendente_equivale_a_nivel_1_sem_mudar_a_ficha(app):
    f = _funcionario()
    estado = svc.snapshot(f)
    assert estado == {'cargo_id': f.cargo_id, 'cargo_nome': 'Atendente 1',
                      'familia': 'Atendimento', 'nivel': 1}
    assert f.cargo.nome == f.funcao == 'Atendente'
    assert f.salario_base == 2200


def test_snapshot_nunca_usa_nivel_sugerido_do_enquadramento(app):
    f = _funcionario('Auxiliar')
    imp = PlanoCarreiraImportacao(nome_arquivo='plano.xlsx', sha256='b' * 64)
    db.session.add(imp)
    db.session.flush()
    db.session.add(PlanoCarreiraEnquadramento(
        importacao_id=imp.id, funcionario_id=f.id, familia='Atendimento',
        nivel=5, cargo_proposto='Gerente', decisao='Proposta final'))
    db.session.commit()
    assert svc.snapshot(f)['nivel'] is None
    assert svc.snapshot(f)['cargo_nome'] == 'Auxiliar'


def test_snapshot_usa_faixa_do_cargo_real_e_nao_numero_no_nome(app):
    f = _funcionario('Atendente 2')
    assert svc.snapshot(f)['nivel'] is None
    _faixa(f.cargo)
    assert svc.snapshot(f)['nivel'] == 2


def test_snapshot_faixas_conflitantes_ficam_desconhecidas(app):
    f = _funcionario()
    _faixa(f.cargo, nivel=1)
    alias = Cargo(nome='Atendente 1', salario_base=2200)
    db.session.add(alias)
    db.session.commit()
    _faixa(alias, nivel=2)
    assert svc.snapshot(f)['nivel'] is None
    assert svc.snapshot(f)['familia'] is None


def test_snapshot_respeita_cargo_id_alterado_com_relacao_carregada(app):
    f = _funcionario()
    cargo = Cargo(nome='Padeiro', salario_base=2500)
    db.session.add(cargo)
    db.session.commit()
    assert f.cargo.nome == 'Atendente'
    f.cargo_id = cargo.id
    assert svc.snapshot(f)['cargo_nome'] == 'Padeiro'


def test_snapshot_respeita_relacao_alterada_antes_do_flush(app):
    f = _funcionario()
    cargo = Cargo(nome='Padeiro', salario_base=2500)
    db.session.add(cargo)
    db.session.commit()
    f.cargo = cargo
    assert svc.snapshot(f)['cargo_nome'] == 'Padeiro'


def test_edicao_sem_troca_e_alias_nao_sao_movimentacoes(app):
    f = _funcionario()
    antes = svc.snapshot(f)
    f.observacao = 'Atualização de contato'
    assert svc.registrar_mudanca(f, antes, origem='ficha_rh') is None
    alias = Cargo(nome='Atendente 1', salario_base=2200)
    db.session.add(alias)
    db.session.flush()
    f.cargo = alias
    assert svc.registrar_mudanca(f, antes, origem='consolidacao_cargos') is None
    assert RhMovimentacao.query.count() == 0


def test_troca_real_grava_snapshot_sem_afirmar_promocao(app, admin_user):
    f = _funcionario()
    antes = svc.snapshot(f)
    cargo = Cargo(nome='Atendente 2', salario_base=2500)
    db.session.add(cargo)
    db.session.commit()
    _faixa(cargo)
    f.cargo = cargo
    evento = svc.registrar_mudanca(
        f, antes, actor_id=admin_user.id, origem='ficha_rh')
    db.session.commit()
    assert evento.cargo_anterior == 'Atendente 1'
    assert evento.nivel_anterior == 1
    assert evento.cargo_novo == 'Atendente 2'
    assert evento.nivel_novo == 2
    assert evento.tipo == 'alteracao'
    assert evento.registrado_em is not None
    assert evento.data_efetiva is None
    assert evento.registrado_por_nome == admin_user.nome
    assert f.salario_base == 2200  # O serviço não altera a remuneração.
    assert svc.ultimas_promocoes([f.id]) == {}


def test_mesma_aprovacao_repetida_nao_inventa_evento(app):
    f = _funcionario()
    antes = svc.snapshot(f)
    assert svc.registrar_mudanca(
        f, antes, origem='plano_carreira', tipo='aplicacao_plano') is None
    assert RhMovimentacao.query.count() == 0


def test_promocao_exige_classificacao_explicita_e_data_pode_ser_desconhecida(app):
    f = _funcionario()
    antes = svc.snapshot(f)
    cargo = Cargo(nome='Atendente 2', salario_base=2500)
    db.session.add(cargo)
    db.session.commit()
    f.cargo = cargo
    evento = svc.registrar_mudanca(
        f, antes, origem='ficha_rh', tipo='promocao')
    db.session.commit()
    assert evento.tipo == 'promocao' and evento.data_efetiva is None
    assert svc.ultimas_promocoes([f.id])[f.id].id == evento.id


def test_historia_registra_data_fornecida_sem_alterar_cargo_nem_salario(app, admin_user):
    f = _funcionario()
    estado = svc.snapshot(f)
    data = date(2024, 5, 9)
    evento = svc.registrar_promocao_historica(
        f, data, actor_id=admin_user.id, observacao='Data conferida pelo RH')
    db.session.commit()
    assert svc.snapshot(f) == estado
    assert f.salario_base == 2200
    assert evento.data_efetiva == data
    assert evento.cargo_anterior is None
    assert evento.nivel_anterior is None
    assert evento.origem == 'historico_informado'
    assert evento.observacao == 'Data conferida pelo RH'
    repetido = svc.registrar_promocao_historica(f, data, actor_id=admin_user.id)
    db.session.commit()
    assert repetido.id == evento.id
    assert RhMovimentacao.query.count() == 1


@pytest.mark.parametrize('data', [None, '2024-01-01', agora().date() + timedelta(days=1)])
def test_historia_rejeita_data_ausente_invalida_ou_futura(app, admin_user, data):
    f = _funcionario()
    with pytest.raises(ValueError):
        svc.registrar_promocao_historica(f, data, actor_id=admin_user.id)
    assert RhMovimentacao.query.count() == 0


def test_historia_rejeita_funcionario_sem_cargo(app, admin_user):
    f = Funcionario(nome='Sem cargo', cpf='42326669827')
    db.session.add(f)
    db.session.commit()
    with pytest.raises(ValueError, match='cargo atual'):
        svc.registrar_promocao_historica(f, date(2024, 1, 1), actor_id=admin_user.id)


def test_historia_rejeita_data_anterior_a_admissao(app, admin_user):
    f = _funcionario()
    f.data_admissao = date(2025, 1, 1)
    db.session.commit()
    with pytest.raises(ValueError, match='admissão'):
        svc.registrar_promocao_historica(f, date(2024, 1, 1), actor_id=admin_user.id)
    assert RhMovimentacao.query.count() == 0


def test_historia_limita_observacao(app, admin_user):
    f = _funcionario()
    with pytest.raises(ValueError, match='2000'):
        svc.registrar_promocao_historica(
            f, date(2024, 1, 1), actor_id=admin_user.id, observacao='x' * 2001)
    assert RhMovimentacao.query.count() == 0


def test_snapshot_sem_cargo_nao_afirma_faixa_apenas_pela_funcao(app):
    f = _funcionario('Atendente 2')
    _faixa(f.cargo)
    f.cargo = None
    db.session.commit()
    estado = svc.snapshot(f)
    assert estado['cargo_nome'] == 'Atendente 2'
    assert estado['cargo_id'] is None
    assert estado['nivel'] is None and estado['familia'] is None


def test_historico_sobrevive_a_remocao_do_plano_e_renomeacao_do_cargo(app, admin_user):
    f = _funcionario('Atendente 2')
    _faixa(f.cargo)
    evento = svc.registrar_promocao_historica(
        f, date(2024, 2, 1), actor_id=admin_user.id)
    db.session.commit()
    PlanoCarreiraCargoVinculo.query.delete()
    PlanoCarreiraFaixa.query.delete()
    PlanoCarreiraImportacao.query.delete()
    f.cargo.nome = 'Nome posterior'
    db.session.commit()
    db.session.expire_all()
    assert evento.cargo_novo == 'Atendente 2'
    assert evento.familia_nova == 'Atendimento' and evento.nivel_novo == 2


def test_ultima_promocao_usa_data_efetiva_nao_ordem_de_digitacao(app, admin_user):
    f = _funcionario()
    recente = svc.registrar_promocao_historica(
        f, date(2025, 3, 1), actor_id=admin_user.id)
    db.session.commit()
    svc.registrar_promocao_historica(f, date(2024, 2, 1), actor_id=admin_user.id)
    db.session.commit()
    assert svc.ultimas_promocoes([f.id])[f.id].id == recente.id
    assert svc.ultimas_promocoes([]) == {}


def test_rollback_da_edicao_remove_tambem_seu_historico(app):
    f = _funcionario()
    antes = svc.snapshot(f)
    cargo = Cargo(nome='Padeiro', salario_base=2500)
    db.session.add(cargo)
    db.session.commit()
    f.cargo = cargo
    svc.registrar_mudanca(f, antes, origem='ficha_rh')
    db.session.rollback()
    assert f.cargo.nome == 'Atendente'
    assert RhMovimentacao.query.count() == 0

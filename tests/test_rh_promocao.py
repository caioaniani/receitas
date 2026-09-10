"""Promoção explícita preserva vínculos e une cargo/plano/histórico na transação."""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    Cargo,
    Funcionario,
    Loja,
    PlanoCarreiraCargoVinculo,
    PlanoCarreiraEnquadramento,
    PlanoCarreiraFaixa,
    PlanoCarreiraImportacao,
    RhMovimentacao,
    Usuario,
)
from app.services import rh_promocao as svc
from app.utils import hoje


def _dados():
    cargo = Cargo(nome='ATENDENTE', salario_base=2330.40, ativo=True)
    destino = Cargo(nome='Atendente 2', salario_base=2500, ativo=True)
    usuario = Usuario(nome='Ana teste', login='ana-promocao', papel='funcionario')
    usuario.set_senha('teste')
    loja = Loja(nome='Unidade Teste', ativa=True)
    lider = Funcionario(nome='Líder teste', cpf='72100000001', ativo=True)
    funcionario = Funcionario(
        nome='Ana teste', cpf='72100000002', cargo=cargo, funcao=cargo.nome,
        salario_base=1000, ativo=True, data_admissao=date(2020, 1, 1),
        cargo_confianca=789, tem_cargo_confianca=True, premiacao=600,
        hora_extra_pct=55, horas_extras=13, vt_dia=10.8, vr_dia=22,
        dias_trabalhados=24, usuario=usuario, lider=lider, lojas=[loja],
        periodo='Manhã', funcao_operacional='Atendimento',
        observacao='Registro anterior', email='ana@example.test')
    db.session.add_all([cargo, destino, usuario, loja, lider, funcionario])
    db.session.commit()
    return funcionario, destino


def _plano(funcionario, destino):
    imp = PlanoCarreiraImportacao(nome_arquivo='plano.xlsx', sha256='c' * 64)
    db.session.add(imp)
    db.session.flush()
    faixa = PlanoCarreiraFaixa(
        importacao_id=imp.id, familia='Atendimento', nivel=2,
        cargo_proposto=destino.nome, equivalente_mensal=2600,
        complemento_funcao=200, total_alvo=2800)
    enquadramento = PlanoCarreiraEnquadramento(
        importacao_id=imp.id, funcionario_id=funcionario.id,
        familia='Atendimento', nivel=1, cargo_proposto='ATENDENTE',
        salario_base_alvo=2200, complemento_funcao_alvo=0, total_alvo=2200,
        cargo_atual_planilha='ATENDENTE', salario_base_atual=2000,
        total_atual=2100, decisao='Em avaliação')
    db.session.add_all([faixa, enquadramento])
    db.session.flush()
    db.session.add(PlanoCarreiraCargoVinculo(cargo=destino, faixa=faixa))
    db.session.commit()
    return enquadramento, faixa


def _campos_preservados(f):
    return {
        key: getattr(f, key) for key in (
            'usuario_id', 'lider_id', 'periodo', 'funcao_operacional',
            'data_admissao', 'cargo_confianca', 'tem_cargo_confianca',
            'premiacao', 'hora_extra_pct', 'horas_extras', 'vt_dia', 'vr_dia',
            'dias_trabalhados', 'observacao', 'email')
    } | {'lojas': [loja.id for loja in f.lojas],
         'papel_usuario': f.usuario.papel, 'senha_usuario': f.usuario.senha_hash}


def test_previa_usa_salario_efetivo_e_preserva_regra_confianca_sem_gravar(app):
    f, destino = _dados()
    antes = (f.cargo_id, f.funcao, f.salario_base, _campos_preservados(f))
    previa = svc.prever(f, destino, date(2025, 5, 2), observacao=' Conferido ')
    assert previa['cargo_atual'] == 'Atendente 1'
    assert previa['cargo_novo'] == 'Atendente 2'
    assert previa['salario_base_atual'] == Decimal('2330.40')
    assert previa['salario_base_novo'] == Decimal('2500.00')
    assert previa['confianca_atual'] == Decimal('932.16')
    assert previa['confianca_nova'] == Decimal('1000.00')
    assert previa['premiacao'] == Decimal('600.00')
    assert previa['total_referencia_atual'] == Decimal('3862.56')
    assert previa['total_referencia_novo'] == Decimal('4100.00')
    assert previa['observacao'] == 'Conferido'
    assert previa['plano_sera_sincronizado'] is False
    assert (f.cargo_id, f.funcao, f.salario_base, _campos_preservados(f)) == antes
    assert not db.session.dirty
    assert RhMovimentacao.query.count() == 0


def test_promocao_sincroniza_plano_preserva_campos_e_grava_historico(app, owner_user):
    f, destino = _dados()
    enquadramento, faixa = _plano(f, destino)
    preservados = _campos_preservados(f)
    cargo_anterior_id = f.cargo_id
    previa = svc.prever(f, destino, date(2025, 5, 2))
    assert previa['plano_sera_sincronizado'] is True
    assert previa['faixa_destino'] == faixa
    resultado = svc.aplicar(
        f, destino, date(2025, 5, 2), actor_id=owner_user.id,
        observacao='Promoção decidida pelo dono')
    assert resultado['evento'] is not None
    db.session.commit()
    db.session.expire_all()
    assert f.cargo_id == destino.id and f.cargo == destino
    assert f.funcao == destino.nome
    assert f.salario_base == f.salario_efetivo() == 2500
    assert _campos_preservados(f) == preservados
    assert enquadramento.familia == 'Atendimento' and enquadramento.nivel == 2
    assert enquadramento.cargo_proposto == destino.nome
    assert enquadramento.decisao == 'Aprovado'
    # O plano guarda a referência da faixa; a base vigente vem do Cargo.
    assert enquadramento.salario_base_alvo == 2600
    assert enquadramento.complemento_funcao_alvo == 200
    assert enquadramento.total_alvo == 2800
    assert enquadramento.salario_base_atual == 2000
    assert enquadramento.total_atual == 2100
    evento = RhMovimentacao.query.one()
    assert evento.tipo == 'promocao' and evento.origem == 'promocao_equipe'
    assert evento.data_efetiva == date(2025, 5, 2)
    assert evento.registrado_por_id == owner_user.id
    assert evento.registrado_por_nome == owner_user.nome
    assert evento.cargo_anterior_id == cargo_anterior_id
    assert evento.cargo_anterior == 'Atendente 1' and evento.nivel_anterior == 1
    assert evento.cargo_novo_id == destino.id
    assert evento.cargo_novo == 'Atendente 2' and evento.nivel_novo == 2
    assert evento.observacao == 'Promoção decidida pelo dono'


def test_sem_commit_do_servico_rollback_desfaz_cargo_plano_e_historico(app, owner_user):
    f, destino = _dados()
    enquadramento, _ = _plano(f, destino)
    cargo_original = f.cargo_id
    svc.aplicar(f, destino, date(2025, 5, 2), actor_id=owner_user.id)
    db.session.flush()
    db.session.rollback()
    assert f.cargo_id == cargo_original and f.funcao == 'ATENDENTE'
    assert f.salario_base == 1000
    assert enquadramento.nivel == 1 and enquadramento.decisao == 'Em avaliação'
    assert RhMovimentacao.query.count() == 0


@pytest.mark.parametrize('data', [None, '2025-05-02', datetime(2025, 5, 2),
                                 date(2019, 12, 31)])
def test_data_invalida_nao_muda_nada_mesmo_se_chamador_fizer_commit(
        app, owner_user, data):
    f, destino = _dados()
    enquadramento, _ = _plano(f, destino)
    cargo_original = f.cargo_id
    with pytest.raises(ValueError):
        svc.aplicar(f, destino, data, actor_id=owner_user.id)
    db.session.commit()
    assert f.cargo_id == cargo_original and f.salario_base == 1000
    assert enquadramento.nivel == 1
    assert RhMovimentacao.query.count() == 0


def test_data_futura_nao_e_promocao_agendada(app, owner_user):
    f, destino = _dados()
    with pytest.raises(ValueError, match='futuro'):
        svc.aplicar(f, destino, hoje() + timedelta(days=1), actor_id=owner_user.id)
    assert f.cargo.nome == 'ATENDENTE'


@pytest.mark.parametrize('inativo', ['funcionario', 'cargo'])
def test_inativos_nao_podem_ser_promovidos(app, owner_user, inativo):
    f, destino = _dados()
    alvo = f if inativo == 'funcionario' else destino
    alvo.ativo = False
    db.session.commit()
    with pytest.raises(ValueError, match='ativ'):
        svc.aplicar(f, destino, date(2025, 5, 2), actor_id=owner_user.id)
    assert f.cargo.nome == 'ATENDENTE'
    assert RhMovimentacao.query.count() == 0


def test_cargo_igual_ou_alias_nao_sao_promocao(app, owner_user):
    f, destino = _dados()
    alias = Cargo(nome='Atendente 1', salario_base=3000, ativo=True)
    inativo = Cargo(nome='Atendente 3', salario_base=3200, ativo=False)
    db.session.add_all([alias, inativo])
    db.session.commit()
    for cargo in (f.cargo, alias):
        with pytest.raises(ValueError, match='diferente'):
            svc.aplicar(f, cargo, date(2025, 5, 2), actor_id=owner_user.id)
    assert svc.cargos_disponiveis(f) == [destino]
    assert f.salario_efetivo() == 2330.4
    assert RhMovimentacao.query.count() == 0


@pytest.mark.parametrize('actor', ['ausente', 'inexistente', 'admin'])
def test_aplicacao_exige_dono_antes_de_mudar_cargo(app, admin_user, actor):
    f, destino = _dados()
    actor_id = {'ausente': None, 'inexistente': 999999, 'admin': admin_user.id}[actor]
    with pytest.raises(ValueError, match='dono'):
        svc.aplicar(f, destino, date(2025, 5, 2), actor_id=actor_id)
    db.session.commit()
    assert f.cargo.nome == 'ATENDENTE'
    assert RhMovimentacao.query.count() == 0


def test_observacao_invalida_nao_muda_nada(app, owner_user):
    f, destino = _dados()
    with pytest.raises(ValueError, match='2000'):
        svc.aplicar(f, destino, date(2025, 5, 2), actor_id=owner_user.id,
                    observacao='x' * 2001)
    db.session.commit()
    assert f.cargo.nome == 'ATENDENTE'
    assert RhMovimentacao.query.count() == 0


def test_promover_nao_cria_plano_nem_muda_salario_de_outra_pessoa(app, owner_user):
    f, destino = _dados()
    colega = Funcionario(nome='Colega teste', cpf='72100000003', cargo=f.cargo,
                        salario_base=1900, ativo=True)
    db.session.add(colega)
    db.session.commit()
    svc.aplicar(f, destino, date(2025, 5, 2), actor_id=owner_user.id)
    db.session.commit()
    assert PlanoCarreiraEnquadramento.query.count() == 0
    assert colega.cargo.nome == 'ATENDENTE'
    assert colega.salario_efetivo() == 2330.4 and colega.salario_base == 1900


def test_repetir_destino_confirmado_nao_duplica_historico(app, owner_user):
    f, destino = _dados()
    svc.aplicar(f, destino, date(2025, 5, 2), actor_id=owner_user.id)
    db.session.commit()
    with pytest.raises(ValueError, match='diferente'):
        svc.aplicar(f, destino, date(2025, 5, 2), actor_id=owner_user.id)
    assert RhMovimentacao.query.count() == 1


def test_vinculo_ambiguo_no_plano_nao_escolhe_primeira_faixa(app, owner_user):
    f, destino = _dados()
    enquadramento, faixa = _plano(f, destino)
    outra = PlanoCarreiraFaixa(
        importacao_id=faixa.importacao_id, familia='Outra família', nivel=2,
        cargo_proposto=destino.nome)
    db.session.add(outra)
    db.session.flush()
    db.session.add(PlanoCarreiraCargoVinculo(cargo=destino, faixa=outra))
    db.session.commit()
    with pytest.raises(ValueError, match='mais de uma faixa'):
        svc.aplicar(f, destino, date(2025, 5, 2), actor_id=owner_user.id)
    assert f.cargo.nome == 'ATENDENTE'
    assert enquadramento.nivel == 1
    assert RhMovimentacao.query.count() == 0


def test_enquadramento_existente_exige_faixa_para_o_novo_cargo(app, owner_user):
    f, destino = _dados()
    enquadramento, faixa = _plano(f, destino)
    PlanoCarreiraCargoVinculo.query.filter_by(faixa_id=faixa.id).delete()
    db.session.commit()
    with pytest.raises(ValueError, match='Corrija o vínculo'):
        svc.aplicar(f, destino, date(2025, 5, 2), actor_id=owner_user.id)
    db.session.commit()
    assert f.cargo.nome == 'ATENDENTE'
    assert enquadramento.nivel == 1
    assert RhMovimentacao.query.count() == 0


def test_confianca_desligada_nao_e_ativada_por_cargo_promovido(app, owner_user):
    f, destino = _dados()
    f.tem_cargo_confianca = False
    db.session.commit()
    previa = svc.aplicar(f, destino, date(2025, 5, 2), actor_id=owner_user.id)
    db.session.commit()
    assert previa['confianca_atual'] == previa['confianca_nova'] == 0
    assert previa['total_referencia_novo'] == Decimal('3100.00')
    assert f.tem_cargo_confianca is False and f.cargo_confianca == 789
    assert f.valor_cargo_confianca() == 0


def test_funcao_legada_equivalente_nao_vira_promocao(app, owner_user):
    f, _ = _dados()
    cargo = f.cargo
    f.cargo = None
    db.session.commit()
    with pytest.raises(ValueError, match='diferente'):
        svc.aplicar(f, cargo, date(2025, 5, 2), actor_id=owner_user.id)
    assert f.cargo is None and f.funcao == 'ATENDENTE'
    assert RhMovimentacao.query.count() == 0

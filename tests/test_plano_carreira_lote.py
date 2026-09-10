"""Aprovação explícita de um lote mantém cadastro, proposta e histórico coerentes."""
from datetime import date, timedelta
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
from app.services import plano_carreira_lote as svc


def _dados():
    atual = Cargo(nome='ATENDENTE', salario_base=2330.40, ativo=True)
    destino = Cargo(nome='Atendente 2', salario_base=2500, ativo=True)
    lider = Funcionario(nome='Líder teste', cpf='74100000001', ativo=True)
    loja = Loja(nome='Unidade de teste', ativa=True)
    conta = Usuario(nome='Ana teste', login='ana-lote', papel='funcionario')
    conta.set_senha('senha de teste')
    ana = Funcionario(
        nome='Ana teste', cpf='74100000002', cargo=atual, funcao=atual.nome,
        salario_base=1000, ativo=True, data_admissao=date(2020, 1, 1),
        premiacao=600, tem_cargo_confianca=True, cargo_confianca=999,
        hora_extra_pct=55, horas_extras=9, vt_dia=10.8, vr_dia=22,
        dias_trabalhados=24, lojas=[loja], lider=lider, usuario=conta,
        email='ana@example.test', telefone='11999999999',
        periodo='Manhã', funcao_operacional='Atendimento', observacao='Preservar')
    bia = Funcionario(nome='Bia teste', cpf='74100000003', cargo=destino,
                      funcao=destino.nome, salario_base=900, ativo=True,
                      premiacao=100, tem_cargo_confianca=False)
    fora = Funcionario(nome='Fora da trilha', cpf='74100000004',
                       funcao='Especialista', salario_base=4000, ativo=True,
                       premiacao=300, tem_cargo_confianca=False)
    importacao = PlanoCarreiraImportacao(nome_arquivo='plano.xlsx', sha256='d' * 64)
    db.session.add_all([atual, destino, lider, loja, conta, ana, bia, fora, importacao])
    db.session.flush()
    faixa = PlanoCarreiraFaixa(
        importacao_id=importacao.id, familia='Atendimento', nivel=2,
        cargo_proposto=destino.nome, equivalente_mensal=2600,
        complemento_funcao=200, total_alvo=2800)
    db.session.add(faixa)
    db.session.flush()
    vinculo = PlanoCarreiraCargoVinculo(cargo=destino, faixa=faixa)
    enquadramentos = [PlanoCarreiraEnquadramento(
        importacao_id=importacao.id, funcionario_id=f.id,
        familia='Atendimento' if f != fora else 'Fora da trilha',
        nivel=2 if f != fora else None,
        cargo_proposto=destino.nome if f != fora else 'Especialista',
        salario_base_atual=2130.4, total_atual=2730.4,
        salario_base_alvo=2600, complemento_funcao_alvo=200, total_alvo=2800,
        decisao='Proposta final' if f == bia else None)
        for f in (ana, bia, fora)]
    db.session.add_all([vinculo, *enquadramentos])
    db.session.commit()
    return enquadramentos, destino, faixa, vinculo, importacao


def _preservados(f):
    return {key: getattr(f, key) for key in (
        'nome', 'cpf', 'ativo', 'premiacao', 'tem_cargo_confianca', 'cargo_confianca',
        'hora_extra_pct', 'horas_extras', 'vt_dia', 'vr_dia', 'dias_trabalhados',
        'data_admissao', 'lojas', 'lider_id', 'usuario_id', 'email', 'telefone',
        'periodo', 'funcao_operacional', 'observacao')} | {
        'papel_conta': f.usuario.papel if f.usuario else None,
        'senha_conta': f.usuario.senha_hash if f.usuario else None,
    }


def test_previa_sem_escrita_mostra_valores_vigentes_e_planilha_separados(app):
    es, destino, faixa, _, _ = _dados()
    dados_antes = [(e.decisao, e.funcionario.cargo_id, e.funcionario.salario_base) for e in es]
    previa = svc.prever([str(es[2].id), str(es[0].id), str(es[1].id)])
    ana, bia, fora = previa['linhas']
    assert previa['ids'] == [e.id for e in es]
    assert ana['cargo_atual'] == 'Atendente 1' and ana['cargo_novo'] == 'Atendente 2'
    assert ana['salario_base_atual'] == Decimal('2330.40')
    assert ana['salario_base_novo'] == Decimal('2500.00')
    assert ana['confianca_atual'] == Decimal('932.16')
    assert ana['confianca_nova'] == Decimal('1000.00')
    assert ana['premiacao'] == Decimal('600.00')
    assert ana['total_referencia_atual'] == Decimal('3862.56')
    assert ana['total_referencia_novo'] == Decimal('4100.00')
    assert ana['total_alvo_planilha'] == Decimal('2800.00')
    assert ana['diferenca_referencia'] == Decimal('237.44')
    assert bia['confianca_atual'] == bia['confianca_nova'] == 0
    assert fora['somente_decisao'] is True
    assert fora['cargo_atual'] == fora['cargo_novo'] == 'Especialista'
    assert fora['total_referencia_atual'] == fora['total_referencia_novo'] == 4300
    assert previa['resumo'] == {
        'quantidade': 3, 'com_cargo': 2, 'somente_decisao': 1,
        'cargos_alterados': 1, 'salarios_alterados': 1,
        'total_referencia_atual': Decimal('10762.56'),
        'total_referencia_novo': Decimal('11000.00'),
        'diferenca_referencia': Decimal('237.44'),
    }
    assert len(previa['estado']) == 64
    assert dados_antes == [(e.decisao, e.funcionario.cargo_id, e.funcionario.salario_base) for e in es]
    assert not db.session.dirty and not db.session.new
    assert RhMovimentacao.query.count() == 0


def test_aprovar_lote_aplica_cargo_e_audita_inclusive_mesmo_cargo_e_fora_trilha(app, owner_user):
    es, destino, _, _, _ = _dados()
    preservados = [_preservados(e.funcionario) for e in es]
    plano_antes = [(e.total_atual, e.total_alvo, e.salario_base_alvo, e.nivel) for e in es]
    cargo_anterior = es[0].funcionario.cargo_id
    previa = svc.prever([e.id for e in es])
    resultado = svc.confirmar(previa['ids'], previa['estado'], owner_user.id)
    db.session.commit()
    db.session.expire_all()
    assert resultado == previa
    assert all(e.decisao == 'Aprovado' for e in es)
    assert es[0].funcionario.cargo_id == es[1].funcionario.cargo_id == destino.id
    assert es[0].funcionario.funcao == es[1].funcionario.funcao == destino.nome
    assert es[0].funcionario.salario_base == es[1].funcionario.salario_base == 2500
    assert es[2].funcionario.cargo_id is None and es[2].funcionario.salario_base == 4000
    assert [_preservados(e.funcionario) for e in es] == preservados
    assert [(e.total_atual, e.total_alvo, e.salario_base_alvo, e.nivel) for e in es] == plano_antes
    assert Cargo.query.count() == 2 and destino.salario_base == 2500
    assert PlanoCarreiraCargoVinculo.query.count() == 1
    eventos = RhMovimentacao.query.order_by(RhMovimentacao.id).all()
    assert len(eventos) == 3
    for e, evento in zip(es, eventos):
        assert evento.funcionario_id == e.funcionario_id
        assert evento.registrado_por_id == owner_user.id
        assert evento.registrado_por_nome == owner_user.nome
        assert evento.tipo == 'aplicacao_plano' and evento.origem == 'aprovacao_lote'
        assert evento.data_efetiva is None
    assert eventos[0].cargo_anterior_id == cargo_anterior and eventos[0].cargo_novo_id == destino.id
    assert eventos[1].cargo_anterior_id == eventos[1].cargo_novo_id == destino.id
    assert eventos[2].cargo_anterior == eventos[2].cargo_novo == 'Especialista'


def test_rollback_do_chamador_desfaz_lote_inteiro_sem_commit_no_servico(app, owner_user):
    es, _, _, _, _ = _dados()
    anteriores = [(e.decisao, e.funcionario.cargo_id, e.funcionario.salario_base) for e in es]
    previa = svc.prever([e.id for e in es])
    svc.confirmar(previa['ids'], previa['estado'], owner_user.id)
    assert RhMovimentacao.query.count() == 3
    db.session.rollback()
    assert [(e.decisao, e.funcionario.cargo_id, e.funcionario.salario_base) for e in es] == anteriores
    assert RhMovimentacao.query.count() == 0


@pytest.mark.parametrize('ids', [None, [], (), '1', [True], [1.5], ['1.0'], ['-1'],
                                [0], [-1], ['1 '], ['١'], ['9' * 11], [2147483648]])
def test_ids_invalidos_recusados_sem_escrita(app, owner_user, ids):
    es, _, _, _, _ = _dados()
    with pytest.raises(ValueError):
        svc.prever(ids)
    with pytest.raises(ValueError):
        svc.confirmar(ids, 'a' * 64, owner_user.id)
    db.session.commit()
    assert es[0].decisao is None and RhMovimentacao.query.count() == 0


def test_recusa_duplicatas_ausentes_e_lote_excessivo(app, owner_user):
    es, _, _, _, _ = _dados()
    for ids in ([es[0].id, str(es[0].id)], [es[0].id, 999999], list(range(1, 202))):
        with pytest.raises(ValueError):
            svc.confirmar(ids, 'a' * 64, owner_user.id)
    db.session.commit()
    assert es[0].decisao is None and RhMovimentacao.query.count() == 0


@pytest.mark.parametrize('bloqueio', ['pessoa_inativa', 'aprovado', 'cargo_inativo',
                                     'sem_vinculo', 'sem_faixa', 'importacao_divergente'])
def test_um_bloqueio_recusa_todo_lote_sem_aceitar_parte(app, owner_user, bloqueio):
    es, destino, faixa, vinculo, importacao = _dados()
    anterior = es[0].funcionario.cargo_id
    previa = svc.prever([e.id for e in es])
    if bloqueio == 'pessoa_inativa':
        es[1].funcionario.ativo = False
    elif bloqueio == 'aprovado':
        es[1].decisao = 'Aprovado'
    elif bloqueio == 'cargo_inativo':
        destino.ativo = False
    elif bloqueio == 'sem_vinculo':
        db.session.delete(vinculo)
    elif bloqueio == 'sem_faixa':
        es[1].nivel = 5
    else:
        outra = PlanoCarreiraImportacao(nome_arquivo='outro.xlsx', sha256='e' * 64)
        db.session.add(outra)
        db.session.flush()
        es[1].importacao_id = outra.id
    db.session.commit()
    with pytest.raises(ValueError):
        svc.confirmar(previa['ids'], previa['estado'], owner_user.id)
    db.session.commit()
    assert es[0].decisao is None and es[0].funcionario.cargo_id == anterior
    assert RhMovimentacao.query.count() == 0


@pytest.mark.parametrize('mudanca', ['nome', 'cargo_atual', 'base_atual', 'base_cache',
                                    'base_destino', 'premiacao', 'confianca', 'decisao',
                                    'total_alvo', 'faixa', 'vinculo', 'sha', 'importado_em'])
def test_revisao_obsoleta_detecta_estado_bruto_antes_de_escrever(app, owner_user, mudanca):
    es, destino, faixa, vinculo, importacao = _dados()
    previa = svc.prever([e.id for e in es])
    f = es[0].funcionario
    if mudanca == 'nome':
        f.nome = 'Nome atualizado'
    elif mudanca == 'cargo_atual':
        f.cargo = destino
    elif mudanca == 'base_atual':
        f.cargo.salario_base += 0.001
    elif mudanca == 'base_cache':
        f.salario_base += 0.001
    elif mudanca == 'base_destino':
        destino.salario_base += 0.001
    elif mudanca == 'premiacao':
        f.premiacao += 0.001
    elif mudanca == 'confianca':
        f.tem_cargo_confianca = False
    elif mudanca == 'decisao':
        es[0].decisao = 'Em avaliação'
    elif mudanca == 'total_alvo':
        es[0].total_alvo += 0.001
    elif mudanca == 'faixa':
        faixa.equivalente_mensal += 0.001
    elif mudanca == 'vinculo':
        vinculo.cargo_id = f.cargo_id
    elif mudanca == 'sha':
        importacao.sha256 = 'f' * 64
    else:
        importacao.importado_em += timedelta(seconds=1)
    db.session.commit()
    with pytest.raises(ValueError, match='mudaram'):
        svc.confirmar(previa['ids'], previa['estado'], owner_user.id)
    db.session.commit()
    assert es[1].decisao == 'Proposta final' and es[2].decisao is None
    assert RhMovimentacao.query.count() == 0


@pytest.mark.parametrize('selecionados', [[0], [0, 2]])
def test_ids_da_confirmacao_nao_podem_reduzir_ou_substituir_lote_revisado(app, owner_user, selecionados):
    es, _, _, _, _ = _dados()
    previa = svc.prever([es[0].id, es[1].id])
    with pytest.raises(ValueError, match='mudaram'):
        svc.confirmar([es[i].id for i in selecionados], previa['estado'], owner_user.id)
    db.session.commit()
    assert RhMovimentacao.query.count() == 0
    assert es[0].decisao is None


def test_reenvio_nao_duplica_auditoria_mesmo_antes_do_commit(app, owner_user):
    es, _, _, _, _ = _dados()
    previa = svc.prever([e.id for e in es])
    svc.confirmar(previa['ids'], previa['estado'], owner_user.id)
    with pytest.raises(ValueError, match='Já aprovado'):
        svc.confirmar(previa['ids'], previa['estado'], owner_user.id)
    db.session.commit()
    assert RhMovimentacao.query.count() == 3
    with pytest.raises(ValueError, match='Já aprovado'):
        svc.confirmar(previa['ids'], previa['estado'], owner_user.id)
    assert RhMovimentacao.query.count() == 3


def test_aprovou_reverteu_decisao_estado_antigo_nao_pode_ser_reutilizado(app, owner_user):
    es, _, _, _, _ = _dados()
    # Mesmo cargo antes/depois isola a proteção pela última auditoria.
    e = es[1]
    e.funcionario.salario_base = e.funcionario.salario_efetivo()
    db.session.commit()
    previa = svc.prever([e.id])
    svc.confirmar(previa['ids'], previa['estado'], owner_user.id)
    e.decisao = 'Proposta final'
    db.session.commit()
    with pytest.raises(ValueError, match='mudaram'):
        svc.confirmar(previa['ids'], previa['estado'], owner_user.id)
    assert RhMovimentacao.query.count() == 1


@pytest.mark.parametrize('actor', ['ausente', 'inexistente', 'admin'])
def test_confirmar_exige_dono(app, admin_user, actor):
    es, _, _, _, _ = _dados()
    previa = svc.prever([e.id for e in es])
    actor_id = {'ausente': None, 'inexistente': 999999, 'admin': admin_user.id}[actor]
    with pytest.raises(ValueError, match='dono'):
        svc.confirmar(previa['ids'], previa['estado'], actor_id)
    db.session.commit()
    assert es[0].decisao is None and RhMovimentacao.query.count() == 0


@pytest.mark.parametrize('estado', [None, '', 'b', 'ç' * 64, 'G' * 64])
def test_estado_invalido_recusado(app, owner_user, estado):
    es, _, _, _, _ = _dados()
    with pytest.raises(ValueError, match='Revise'):
        svc.confirmar([e.id for e in es], estado, owner_user.id)
    db.session.commit()
    assert RhMovimentacao.query.count() == 0


def test_fora_trilha_com_cargo_real_mantem_todos_valores(app, owner_user):
    es, destino, _, _, _ = _dados()
    e = es[2]
    e.funcionario.cargo = destino
    e.funcionario.salario_base = 9876
    db.session.commit()
    previa = svc.prever([e.id])
    assert previa['linhas'][0]['salario_base_novo'] == 2500
    assert previa['resumo']['cargos_alterados'] == previa['resumo']['salarios_alterados'] == 0
    svc.confirmar(previa['ids'], previa['estado'], owner_user.id)
    db.session.commit()
    assert e.decisao == 'Aprovado'
    assert e.funcionario.cargo_id == destino.id and e.funcionario.salario_base == 9876
    assert RhMovimentacao.query.one().cargo_novo_id == destino.id


def test_estado_independente_da_ordem_da_selecao(app):
    es, _, _, _, _ = _dados()
    assert svc.prever([e.id for e in es]) == svc.prever([e.id for e in reversed(es)])


def test_helper_de_bloqueio_cobre_mesmos_impedimentos_da_tabela(app):
    es, destino, _, _, _ = _dados()
    assert svc.motivo_bloqueio(es[0], destino) is None
    assert svc.motivo_bloqueio(es[2], None) is None
    assert 'vinculado' in svc.motivo_bloqueio(es[0], None)
    destino.ativo = False
    assert 'inativo' in svc.motivo_bloqueio(es[0], destino)
    es[0].decisao = 'Aprovado'
    assert svc.motivo_bloqueio(es[0], destino) == 'Já aprovado.'
    es[0].funcionario.ativo = False
    assert 'inativo' in svc.motivo_bloqueio(es[0], destino)


@pytest.mark.parametrize('valor', [-1, float('inf'), float('nan'), 1e100])
def test_valores_invalidos_na_previa_bloqueiam_lote(app, valor):
    es, destino, _, _, _ = _dados()
    # Float NaN vira NULL no SQLite; exercício direto da conversão compartilhada.
    from app.services.rh_remuneracao import comparar
    destino.salario_base = valor
    with db.session.no_autoflush, pytest.raises(ValueError, match='remuneração'):
        comparar(es[0].funcionario, destino)
    db.session.rollback()
    assert RhMovimentacao.query.count() == 0

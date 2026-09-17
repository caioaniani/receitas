"""Operações administrativas preservam unidade, residual e histórico da massa."""

from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    EstoqueProducao,
    MateriaPrima,
    MovEstoqueMassa,
    Receita,
    SaldoResidualMassa,
)
from app.services.estoque_massa import registrar_entrada_massa, saldo_bolas
from app.utils import agora


def _receita(nome):
    rec = Receita(nome=nome, categoria='Insumos', peso_base=2000,
                  rendimento_qtd=1, rendimento_unidade='un', peso_unitario=3580)
    db.session.add(rec)
    db.session.flush()
    return rec


def _login(app, usuario):
    cliente = app.test_client()
    cliente.post('/auth/login', data={'login': usuario.login, 'senha': '123'})
    return cliente


@pytest.mark.parametrize('massa_na_origem', [True, False])
def test_mescla_com_massa_controlada_nao_move_saldo_ou_historico(
        app, admin_user, massa_na_origem):
    massa = _receita('Massa para folhar')
    outra = _receita('Outra massa')
    registrar_entrada_massa(massa, 44900, admin_user.id, 'Batimento')
    ep_outra = EstoqueProducao(receita_id=outra.id, quantidade=3)
    db.session.add(ep_outra)
    db.session.commit()
    origem, destino = (massa, outra) if massa_na_origem else (outra, massa)
    cliente = _login(app, admin_user)
    resposta = cliente.post(f'/receitas/{origem.id}/vinculos/transferir',
                            data={'destino': destino.nome})
    assert resposta.status_code == 409
    assert 'histórico de massa' in resposta.get_json()['erro']
    assert EstoqueProducao.query.filter_by(receita_id=massa.id).one().quantidade == 12
    assert EstoqueProducao.query.filter_by(receita_id=outra.id).one().quantidade == 3
    assert db.session.get(SaldoResidualMassa, massa.id).g == Decimal(1940)
    assert MovEstoqueMassa.query.one().receita_id == massa.id


def test_mescla_bloqueada_mesmo_se_resto_zerado_e_receita_renomeada(app, admin_user):
    massa = _receita('Massa para folhar')
    destino = _receita('Destino')
    registrar_entrada_massa(massa, 3580, admin_user.id, 'Bola')
    massa.nome = 'Massa para folhar antiga'
    db.session.commit()
    cliente = _login(app, admin_user)
    resposta = cliente.post(f'/receitas/{massa.id}/vinculos/transferir',
                            data={'destino': destino.nome})
    assert resposta.status_code == 409
    assert db.session.get(SaldoResidualMassa, massa.id).g == 0
    assert MovEstoqueMassa.query.one().receita_id == massa.id


def test_ledger_de_massa_bloqueia_mescla_mesmo_sem_saldo_residual(app, admin_user):
    massa = _receita('Massa para folhar')
    destino = _receita('Destino')
    registrar_entrada_massa(massa, 3580, admin_user.id, 'Bola anterior')
    db.session.commit()
    # Defesa da manutenção: histórico sobrevivente também impede a fusão.
    db.session.delete(db.session.get(SaldoResidualMassa, massa.id))
    db.session.commit()
    resposta = _login(app, admin_user).post(
        f'/receitas/{massa.id}/vinculos/transferir', data={'destino': destino.nome})
    assert resposta.status_code == 409
    assert MovEstoqueMassa.query.one().receita_id == massa.id


def test_transferencia_para_mp_tambem_preserva_historico_da_massa(app, admin_user):
    massa = _receita('Massa para folhar')
    mp = MateriaPrima(nome='Massa comprada', unidade='g', custo_por_kg=10)
    db.session.add(mp)
    registrar_entrada_massa(massa, 44900, admin_user.id, 'Batimento')
    db.session.commit()
    resposta = _login(app, admin_user).post(
        f'/receitas/{massa.id}/vinculos/transferir',
        data={'tipo_destino': 'mp', 'destino': mp.nome})
    assert resposta.status_code == 409
    assert saldo_bolas(massa) == Decimal(44900) / Decimal(3580)


def test_mescla_nao_reaponta_snapshot_de_massa_antes_da_primeira_entrada(app, admin_user):
    from app.models import PlanejamentoItem, PlanejamentoItemBatelada, PlanejamentoProducao
    from app.services.viennoiserie import TIPO_MASSA
    from app.utils import hoje
    massa = _receita('Massa para folhar')
    destino = _receita('Outra receita')
    plano = PlanejamentoProducao(data=hoje(), enviado_ao_padeiro=True)
    item = PlanejamentoItem(receita=massa, qtd_alvo=1)
    item.batelada_padrao = PlanejamentoItemBatelada(
        dados={'tipo': TIPO_MASSA, 'peso_bola_g': 3580}, bateladas=1)
    plano.itens.append(item)
    db.session.add(plano)
    massa.nome = 'Nome modificado após o envio'
    db.session.commit()
    assert SaldoResidualMassa.query.count() == MovEstoqueMassa.query.count() == 0
    resposta = _login(app, admin_user).post(
        f'/receitas/{massa.id}/vinculos/transferir', data={'destino': destino.nome})
    assert resposta.status_code == 409
    db.session.refresh(item)
    assert item.receita_id == massa.id


@pytest.mark.parametrize('gramas', [44900, 1790])
def test_limpeza_arquivada_enxerga_e_zera_residual_com_historico(
        app, owner_user, gramas):
    massa = _receita('Massa para folhar')
    registrar_entrada_massa(massa, gramas, owner_user.id, 'Produção anterior')
    massa.arquivada_em = agora()
    db.session.commit()
    cliente = _login(app, owner_user)
    antes = MovEstoqueMassa.query.count()
    previa = cliente.get('/admin/arquivadas-saldo').get_json()
    assert previa['dry_run'] and previa['total_linhas'] == 1
    assert previa['linhas'][0]['residual_g'] == gramas % 3580
    assert saldo_bolas(massa) == Decimal(gramas) / Decimal(3580)
    assert MovEstoqueMassa.query.count() == antes
    resposta = cliente.get('/admin/arquivadas-saldo?executar=1').get_json()
    assert resposta['zerados'] == 1
    assert EstoqueProducao.query.filter_by(receita_id=massa.id).one().quantidade == 0
    assert db.session.get(SaldoResidualMassa, massa.id).g == 0
    assert saldo_bolas(massa) == 0
    ajuste = MovEstoqueMassa.query.filter_by(tipo='ajuste_conferencia').one()
    assert ajuste.quantidade_g == Decimal(gramas)
    assert ajuste.saldo_posterior_g == 0
    assert 'arquivado' in ajuste.referencia
    # Sem saldo restante, repetir a ação não produz outro ajuste.
    repeticao = cliente.get('/admin/arquivadas-saldo?executar=1').get_json()
    assert repeticao['zerados'] == repeticao['total_linhas'] == 0
    assert MovEstoqueMassa.query.count() == antes + 1


def test_limpeza_nao_zera_massa_ativa(app, owner_user):
    massa = _receita('Massa para folhar')
    registrar_entrada_massa(massa, 1790, owner_user.id, 'Produção ativa')
    db.session.commit()
    resposta = _login(app, owner_user).get('/admin/arquivadas-saldo?executar=1').get_json()
    assert resposta['zerados'] == 0
    assert saldo_bolas(massa) == Decimal('0.5')


def test_limpeza_de_massa_renomeada_conserva_identidade_e_zera_residual(app, owner_user):
    massa = _receita('Massa para folhar')
    registrar_entrada_massa(massa, 44900, owner_user.id, 'Produção')
    massa.nome = 'Nome alterado depois da produção'
    massa.arquivada_em = agora()
    db.session.commit()
    resposta = _login(app, owner_user).get('/admin/arquivadas-saldo?executar=1').get_json()
    assert resposta['zerados'] == 1
    assert not resposta['pulados_com_reserva']
    assert EstoqueProducao.query.filter_by(receita_id=massa.id).one().quantidade == 0
    assert db.session.get(SaldoResidualMassa, massa.id).g == 0
    assert MovEstoqueMassa.query.filter_by(tipo='ajuste_conferencia').one().quantidade_g == 44900

"""Reproduções independentes da revisão: unidade, snapshot e decisões humanas."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest
from test_viennoiserie_integracao import calcular, cenario, linha

from app.extensions import db
from app.models import (
    CronogramaOverride,
    MateriaPrima,
    MovEstoqueMassa,
    PlanejamentoItem,
    PlanejamentoItemBatelada,
    PlanejamentoProducao,
)
from app.services.estoque_massa import peso_bola_g, registrar_entrada_massa, saldo_bolas
from app.services.producao import (
    _sync_itens_do_cronograma,
    enviar_plano_do_dia,
    produzir_item_plano,
)
from app.utils import hoje


@pytest.fixture(autouse=True)
def segunda_com_freeze(congela_hoje, monkeypatch):
    congela_hoje()
    monkeypatch.setenv('FREEZE_INSUMO', '1')


def _enviar_massa(receitas, usuario_id, indice=0):
    quantidades = [0] * (indice + 3)
    quantidades[indice + 1] = 50
    linhas = [linha(receitas[1], quantidades)]
    calculado = calcular(receitas, deepcopy(linhas))
    plano = enviar_plano_do_dia(
        hoje() + timedelta(days=indice), usuario_id,
        crono={'receitas': list(calculado.values())})
    item = next(it for it in plano.itens if it.receita_id == receitas[0].id)
    return item, linhas, calculado


def _ordem_filho(receita, quantidade):
    plano = PlanejamentoProducao(
        data=hoje() + timedelta(days=1), origem='cronograma',
        enviado_ao_padeiro=True)
    if quantidade:
        plano.itens.append(PlanejamentoItem(
            receita=receita, qtd_alvo=quantidade, produzido_qtd=0))
    db.session.add(plano)
    db.session.commit()
    return plano


@pytest.mark.parametrize('quantidade_manual', [0, 3])
def test_override_da_massa_bloqueia_sem_reinterpretar_bolas(
        app, admin_user, quantidade_manual):
    receitas = cenario()
    massa, croissant = receitas[:2]
    db.session.add(CronogramaOverride(
        receita_id=massa.id, data=hoje(), qtd=quantidade_manual))
    db.session.commit()

    resultado = calcular(receitas, [linha(croissant, [0, 50, 0])])

    celula = resultado[massa.id]['por_dia'][0]
    assert celula['qtd'] == 0
    assert 'edição manual' in celula['batelada_bloqueada']
    assert resultado[croissant.id]['por_dia'][1]['qtd'] == 50
    assert 'batelada_padrao' not in celula
    assert enviar_plano_do_dia(
        hoje(), admin_user.id, crono={'receitas': list(resultado.values())}) is None
    assert PlanejamentoItemBatelada.query.count() == 0
    assert MateriaPrima.query.filter_by(nome='FarinhaT45').one().estoque_atual == 200000


@pytest.mark.parametrize('peso_editado', [3000, 4000])
def test_planner_conserva_peso_historico_e_nao_distribui_massa_fantasma(
        app, admin_user, peso_editado):
    receitas = cenario()
    massa, croissant = receitas[:2]
    registrar_entrada_massa(massa, 44900, admin_user.id, 'Batimento anterior')
    db.session.commit()
    massa.peso_unitario = peso_editado
    db.session.commit()

    resultado = calcular(receitas, [linha(croissant, [0, 50, 0])])

    celula = resultado[massa.id]['por_dia'][0]
    produzido = resultado[croissant.id]['por_dia'][1]['qtd']
    peso = peso_bola_g(massa)
    consumo = Decimal(produzido) / Decimal(40) * peso
    assert peso == celula['batelada_padrao']['peso_bola_g'] == 3580
    assert celula['qtd'] == 1
    assert produzido == 501
    assert consumo <= Decimal('44900')
    assert Decimal('44900') - consumo == Decimal('60.5')
    assert Decimal(str(celula['massa_viennoiserie']['residuo_g'])) == Decimal('60.5')


@pytest.mark.parametrize('indice', [0, 2], ids=['hoje', 'futuro'])
@pytest.mark.parametrize('edicao', ['peso_zero', 'sem_farinha', 'outro_nome'])
def test_snapshot_enviado_mantem_batimento_quando_ficha_viva_muda(
        app, admin_user, indice, edicao):
    receitas = cenario()
    massa = receitas[0]
    item, linhas, _calculado = _enviar_massa(receitas, admin_user.id, indice)
    snapshot = deepcopy(item.batelada_padrao.dados)
    if edicao == 'peso_zero':
        massa.peso_unitario = 0
    elif edicao == 'sem_farinha':
        farinha = next(ing for ing in massa.ingredientes
                       if ing.ingrediente_nome == 'FarinhaT45')
        massa.ingredientes.remove(farinha)
        db.session.delete(farinha)
    else:
        massa.nome = 'Massa compartilhada renomeada'
    db.session.commit()

    resultado = calcular(receitas, deepcopy(linhas))

    celula = resultado[massa.id]['por_dia'][indice]
    assert celula['qtd'] == 1
    assert celula['batelada_padrao']['tipo'] == 'massa_viennoiserie'
    assert celula['batelada_padrao']['farinha_g'] == 25000
    assert celula['batelada_padrao']['massa_g'] == 44900
    assert celula['batelada_padrao']['peso_bola_g'] == 3580
    assert 'batelada_bloqueada' not in celula
    assert item.qtd_alvo == 1
    assert item.batelada_padrao.dados == snapshot
    assert PlanejamentoItemBatelada.query.count() == 1


@pytest.mark.parametrize('filho_enviado', [False, True])
def test_override_zero_do_filho_prevalece_sobre_destino_aprovado_da_massa(
        app, admin_user, filho_enviado):
    receitas = cenario()
    massa, croissant = receitas[:2]
    item, _linhas, calculado = _enviar_massa(receitas, admin_user.id)
    amanha = hoje() + timedelta(days=1)
    plano_filho = None
    if filho_enviado:
        plano_filho = enviar_plano_do_dia(
            amanha, admin_user.id, crono={'receitas': list(calculado.values())})
        assert any(it.receita_id == croissant.id for it in plano_filho.itens)
    db.session.add(CronogramaOverride(receita_id=croissant.id, data=amanha, qtd=0))
    db.session.commit()

    resultado = calcular(receitas, [linha(croissant, [0, 0, 0])])

    assert resultado[croissant.id]['por_dia'][1]['qtd'] == 0
    assert resultado[massa.id]['por_dia'][0]['qtd'] == 1
    assert item.qtd_alvo == 1
    if filho_enviado:
        _sync_itens_do_cronograma(
            plano_filho, amanha, 3, 6, 0, False, automatico=False,
            crono={'receitas': list(resultado.values())})
        db.session.commit()
        assert PlanejamentoItem.query.filter_by(
            planejamento_id=plano_filho.id, receita_id=croissant.id).count() == 0


@pytest.mark.parametrize('alvo_enviado', [0, 250])
def test_demanda_tardia_preserva_alvo_e_avisa_quantidade_original(app, alvo_enviado):
    receitas = cenario()
    croissant = receitas[1]
    plano = _ordem_filho(croissant, alvo_enviado)

    resultado = calcular(receitas, [linha(croissant, [0, 400, 0])])

    celula = resultado[croissant.id]['por_dia'][1]
    assert celula['qtd'] == alvo_enviado
    assert celula['qtd_solicitada'] == 400
    _n, congelados = _sync_itens_do_cronograma(
        plano, plano.data, 3, 6, 0, False, automatico=True,
        crono={'receitas': list(resultado.values())})
    db.session.flush()
    assert congelados == [(croissant.nome, alvo_enviado, 400, 1)]
    item = PlanejamentoItem.query.filter_by(
        planejamento_id=plano.id, receita_id=croissant.id).first()
    assert (item.qtd_alvo if item else 0) == alvo_enviado
    assert bool(item) == bool(alvo_enviado)


def test_desligar_freeze_libera_planner_e_escritor_da_ordem(app, monkeypatch):
    receitas = cenario()
    croissant = receitas[1]
    plano = _ordem_filho(croissant, 250)
    monkeypatch.setenv('FREEZE_INSUMO', '0')

    resultado = calcular(receitas, [linha(croissant, [0, 400, 0])])

    celula = resultado[croissant.id]['por_dia'][1]
    assert celula['qtd'] >= 400
    assert not celula.get('batelada_congelada')
    _n, congelados = _sync_itens_do_cronograma(
        plano, plano.data, 3, 6, 0, False, automatico=True,
        crono={'receitas': list(resultado.values())})
    db.session.flush()
    assert congelados == []
    assert PlanejamentoItem.query.filter_by(
        planejamento_id=plano.id, receita_id=croissant.id).one().qtd_alvo == celula['qtd']


@pytest.mark.parametrize('parcelas', [[45], [1] * 45, [7, 11, 27]],
                         ids=['inteiro', 'unitarias', 'irregulares'])
def test_parcelas_da_montagem_conservam_gramas_no_saldo_e_no_historico(
        app, admin_user, parcelas):
    receitas = cenario()
    massa, pain = receitas[0], receitas[2]
    registrar_entrada_massa(massa, 44900, admin_user.id, 'Batimento anterior')
    plano = PlanejamentoProducao(
        data=hoje(), origem='cronograma', enviado_ao_padeiro=True)
    item = PlanejamentoItem(receita=pain, qtd_alvo=45, produzido_qtd=0)
    plano.itens.append(item)
    db.session.add(plano)
    db.session.commit()

    confirmado = 0
    for parcela in parcelas:
        resposta = produzir_item_plano(
            item.id, parcela, admin_user.id, produzido_esperado=confirmado)
        assert resposta['ok']
        confirmado += parcela
        consumo_acumulado = (Decimal(3580) * confirmado / Decimal(45)).quantize(
            Decimal('.000001'))
        saldo = (saldo_bolas(massa) * peso_bola_g(massa)).quantize(Decimal('.000001'))
        assert saldo == Decimal(44900) - consumo_acumulado

    movimentos = MovEstoqueMassa.query.filter_by(
        receita_id=massa.id, tipo='consumo_subreceita').all()
    assert len(movimentos) == len(parcelas)
    assert sum((mov.quantidade_g for mov in movimentos), Decimal(0)) == Decimal(3580)
    assert item.produzido_qtd == 45
    assert not MovEstoqueMassa.query.filter_by(
        receita_id=massa.id, tipo='consumo_subreceita_sem_estoque').count()

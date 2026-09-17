"""Alocação pura de massa comum: prioridade, peças inteiras e conservação."""

from copy import deepcopy
from decimal import Decimal, localcontext
from random import Random

import pytest

from app.services.alocacao_viennoiserie import distribuir_excedente


def candidato(rid, necessidade, gramas=100, nome=None, limite=None):
    return {'receita_id': rid, 'nome': nome or str(rid),
            'gramas_por_unidade': gramas, 'necessidade': necessidade,
            'maximo_adicional': limite}


def test_prioriza_maior_necessidade_atual_sem_divisao_igual():
    candidatos = [candidato(1, 10), candidato(2, 3), candidato(3, 1)]
    assert distribuir_excedente(600, candidatos) == {
        'alocacoes': {1: 6}, 'restante_g': '0'}
    assert distribuir_excedente(1000, candidatos) == {
        'alocacoes': {1: 9, 2: 1}, 'restante_g': '0'}


def test_desempate_nome_id_independe_da_ordem_de_entrada():
    candidatos = [candidato(4, 4, nome='Pain'), candidato(2, 4, nome='danish'),
                  candidato(1, 4, nome='Danish')]
    assert distribuir_excedente(200, candidatos) == {
        'alocacoes': {1: 1, 2: 1}, 'restante_g': '0'}
    assert distribuir_excedente(200, list(reversed(candidatos))) == \
        distribuir_excedente(200, candidatos)


def test_limites_e_pecas_que_nao_cabem_passam_ao_proximo():
    candidatos = [candidato(1, 100, limite=2), candidato(2, 90, gramas=500),
                  candidato(3, 5, gramas=75, limite=2)]
    assert distribuir_excedente(375, candidatos) == {
        'alocacoes': {1: 2, 3: 2}, 'restante_g': '25'}


def test_apos_cobrir_demanda_excedente_segue_maior_necessidade_original():
    candidatos = [candidato(1, 3), candidato(2, 1)]
    assert distribuir_excedente(1000, candidatos) == {
        'alocacoes': {1: 9, 2: 1}, 'restante_g': '0'}


def test_excedente_tambem_respeita_limites_e_preserva_sobra():
    candidatos = [candidato(1, 3, limite=5), candidato(2, 1, limite=2)]
    assert distribuir_excedente(1000, candidatos) == {
        'alocacoes': {1: 5, 2: 2}, 'restante_g': '300'}


def test_candidatos_invalidos_sem_necessidade_e_bloqueados_sao_excluidos():
    candidatos = [candidato(1, 0), candidato(2, -1), candidato(3, 2, gramas=0),
                  candidato(4, 2, limite=0), candidato(5, 'NaN'),
                  candidato(6, 2, gramas='Infinity'), candidato(7, 2, limite='1.5'),
                  candidato(True, 2), candidato(9, True), None,
                  {'id': 10, 'nome': 'Pain', 'gramas_por_unidade': '0.1',
                   'necessidade': 1}]
    assert distribuir_excedente('0.3', candidatos) == {
        'alocacoes': {10: 3}, 'restante_g': '0'}


def test_zero_sem_candidatos_e_residuo_nao_fabricam_unidade():
    assert distribuir_excedente(0, []) == {'alocacoes': {}, 'restante_g': '0'}
    assert distribuir_excedente(0, [candidato(1, 5)]) == {
        'alocacoes': {}, 'restante_g': '0'}
    assert distribuir_excedente('10.050', []) == {'alocacoes': {}, 'restante_g': '10.05'}
    assert distribuir_excedente('0.299999', [candidato(1, 5, gramas='0.1')]) == {
        'alocacoes': {1: 2}, 'restante_g': '0.099999'}


def test_decimal_exato_independente_da_precisao_global():
    with localcontext() as contexto:
        contexto.prec = 3
        assert distribuir_excedente('44900.000001', [candidato(1, 1000, gramas=86)]) == {
            'alocacoes': {1: 522}, 'restante_g': '8.000001'}
        assert distribuir_excedente('0.0000000000000000000000000003', [
            candidato(1, 3, gramas='0.0000000000000000000000000001')]) == {
                'alocacoes': {1: 3}, 'restante_g': '0'}


@pytest.mark.parametrize('massa', [-1, 'NaN', 'Infinity', 'inválida', None, True])
def test_massa_invalida_rejeitada(massa):
    with pytest.raises(ValueError, match='massa disponível'):
        distribuir_excedente(massa, [])


def test_receita_repetida_exige_consolidacao_antes_do_limite():
    with pytest.raises(ValueError, match='consolide seus caminhos'):
        distribuir_excedente(1000, [candidato(1, 2), candidato(1, 3)])


def test_nao_muda_candidatos():
    candidatos = [candidato(1, 12), candidato(2, 5)]
    original = deepcopy(candidatos)
    distribuir_excedente(1000, candidatos)
    assert candidatos == original


def test_conservacao_e_prioridade_contra_selecao_unidade_a_unidade():
    rng = Random(25_000)
    for _ in range(100):
        candidatos = [candidato(rid, str(Decimal(rng.randint(1, 20)) / 2),
                                gramas=str(Decimal(rng.randint(1, 20)) / 10),
                                limite=rng.choice([None, 1, 4, 20]))
                      for rid in range(1, 5)]
        massa = Decimal(rng.randint(1, 150)) / 10
        restante = massa
        contagem = {c['receita_id']: 0 for c in candidatos}
        necessidades = {c['receita_id']: Decimal(c['necessidade']) for c in candidatos}
        while True:
            elegiveis = [c for c in candidatos
                         if Decimal(c['gramas_por_unidade']) <= restante
                         and (c['maximo_adicional'] is None
                              or contagem[c['receita_id']] < c['maximo_adicional'])]
            if not elegiveis:
                break
            pendentes = [c for c in elegiveis if necessidades[c['receita_id']] > 0]
            escolhido = min(pendentes or elegiveis, key=lambda c: (
                -(necessidades[c['receita_id']] if pendentes else Decimal(c['necessidade'])),
                c['nome'].casefold(), c['receita_id']))
            rid = escolhido['receita_id']
            contagem[rid] += 1
            necessidades[rid] = max(Decimal(0), necessidades[rid] - 1)
            restante -= Decimal(escolhido['gramas_por_unidade'])

        resultado = distribuir_excedente(massa, candidatos)
        assert resultado['alocacoes'] == {rid: qtd for rid, qtd in contagem.items() if qtd}
        assert Decimal(resultado['restante_g']) == restante
        consumo = sum(Decimal(c['gramas_por_unidade']) *
                      resultado['alocacoes'].get(c['receita_id'], 0) for c in candidatos)
        assert consumo + restante == massa
        assert restante >= 0
        assert all(isinstance(qtd, int) and qtd > 0 for qtd in resultado['alocacoes'].values())

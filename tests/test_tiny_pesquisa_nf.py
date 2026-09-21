"""Pesquisa de notas para recuperar vínculo sem aceitar resultado parcial."""
from unittest.mock import call, patch

import pytest

from app.services import tiny

DOCUMENTO = '40099645000148'


def _pagina(numero=1, total=1, notas=()):
    return {
        'status': 'OK', 'pagina': numero, 'numero_paginas': total,
        'notas_fiscais': [{'nota_fiscal': nota} for nota in notas],
    }


@pytest.mark.parametrize('numero', ['012.693', '00012693', 12693])
def test_pesquisa_normaliza_numero_cnpj_e_filtra_notas_de_saida(numero):
    nota = {'id': '911314754', 'numero': '12693'}
    with patch('app.services.tiny._get', return_value=_pagina(notas=[nota])) as get:
        assert tiny.pesquisar_notas_fiscais(numero, '40.099.645/0001-48') == [nota]
    get.assert_called_once_with('notas.fiscais.pesquisa.php', {
        'tipoNota': 'S', 'numero': '12693', 'cpf_cnpj': DOCUMENTO, 'pagina': 1,
    }, retornar_erro=True)


def test_pesquisa_coleta_todas_as_paginas_e_deduplica_id():
    primeira = {'id': 10, 'numero': '12693'}
    outra = {'id': '11', 'numero': '12693'}
    repetida = {'id': '10', 'numero': '12693'}
    with patch('app.services.tiny._get', side_effect=[
        _pagina('1', '2', [primeira]), _pagina('2', '2', [outra, repetida]),
    ]) as get:
        notas = tiny.pesquisar_notas_fiscais('12693', DOCUMENTO)
    assert {str(nota['id']) for nota in notas} == {'10', '11'}
    assert len(notas) == 2
    assert get.call_args_list == [
        call('notas.fiscais.pesquisa.php', {
            'tipoNota': 'S', 'numero': '12693', 'cpf_cnpj': DOCUMENTO, 'pagina': pagina,
        }, retornar_erro=True) for pagina in (1, 2)
    ]


@pytest.mark.parametrize('retorno', [
    None, {}, {'status': 'Erro', 'erros': [{'erro': 'Token inválido'}]},
])
def test_erro_da_api_aborta_pesquisa_inclusive_apos_candidato(retorno):
    nota = {'id': '911314754'}
    with patch('app.services.tiny._get', side_effect=[
        _pagina(1, 2, [nota]), retorno,
    ]) as get:
        with pytest.raises(ValueError, match='Não foi possível pesquisar'):
            tiny.pesquisar_notas_fiscais('12693', DOCUMENTO)
    assert get.call_count == 2


@pytest.mark.parametrize('campo,valor', [
    ('pagina', None), ('pagina', 'incerta'), ('pagina', 0), ('pagina', 2),
    ('numero_paginas', None), ('numero_paginas', ''), ('numero_paginas', 'incerta'),
    ('numero_paginas', 0), ('numero_paginas', -1),
])
def test_paginacao_invalida_aborta_mesmo_com_candidato(campo, valor):
    retorno = _pagina(notas=[{'id': '911314754'}])
    retorno[campo] = valor
    with patch('app.services.tiny._get', return_value=retorno) as get:
        with pytest.raises(ValueError, match='não confirmou todas as páginas'):
            tiny.pesquisar_notas_fiscais('12693', DOCUMENTO)
    assert get.call_count == 1


@pytest.mark.parametrize('campo', ['pagina', 'numero_paginas'])
def test_paginacao_ausente_nao_assume_pagina_unica(campo):
    retorno = _pagina(notas=[{'id': '911314754'}])
    del retorno[campo]
    with patch('app.services.tiny._get', return_value=retorno):
        with pytest.raises(ValueError, match='não confirmou todas as páginas'):
            tiny.pesquisar_notas_fiscais('12693', DOCUMENTO)


def test_pagina_repetida_aborta_sem_devolver_resultado_parcial():
    retorno = _pagina(1, 2, [{'id': '911314754'}])
    with patch('app.services.tiny._get', return_value=retorno) as get:
        with pytest.raises(ValueError, match='não confirmou todas as páginas'):
            tiny.pesquisar_notas_fiscais('12693', DOCUMENTO)
    assert get.call_count == 2


@pytest.mark.parametrize('primeiro_total,segundo_total', [(3, 2), (2, 3)])
def test_mudanca_no_total_de_paginas_aborta_pesquisa(primeiro_total, segundo_total):
    with patch('app.services.tiny._get', side_effect=[
        _pagina(1, primeiro_total, [{'id': '911314754'}]),
        _pagina(2, segundo_total, [{'id': 'outra'}]),
    ]) as get:
        with pytest.raises(ValueError, match='não confirmou todas as páginas'):
            tiny.pesquisar_notas_fiscais('12693', DOCUMENTO)
    assert get.call_count == 2


def test_limite_de_paginas_aborta_sem_escolher_nota_parcial():
    with patch('app.services.tiny._get', side_effect=[
        _pagina(pagina, 4, [{'id': str(pagina)}]) for pagina in range(1, 4)
    ]) as get:
        with pytest.raises(ValueError, match='muitas notas'):
            tiny.pesquisar_notas_fiscais('12693', DOCUMENTO)
    assert get.call_count == 3


@pytest.mark.parametrize('numero,documento', [
    (None, DOCUMENTO), ('', DOCUMENTO), ('0', DOCUMENTO), ('000.000', DOCUMENTO),
    ('NF12693', DOCUMENTO), ('-1', DOCUMENTO), ('12693', None), ('12693', ''),
    ('12693', '123'), ('12693', '123456789012345'),
])
def test_entrada_invalida_nao_consulta_rede(numero, documento):
    with patch('app.services.tiny._get') as get:
        with pytest.raises(ValueError, match='número da NF e um CPF/CNPJ válido'):
            tiny.pesquisar_notas_fiscais(numero, documento)
    get.assert_not_called()


def test_pesquisa_vazia_confirmada_retorna_lista_vazia():
    with patch('app.services.tiny._get', return_value=_pagina()) as get:
        assert tiny.pesquisar_notas_fiscais('12693', DOCUMENTO) == []
    assert get.call_count == 1

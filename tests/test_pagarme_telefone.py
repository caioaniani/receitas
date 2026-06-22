"""Normalizacao de telefone BR pro Pagar.me (incidente 22/06/2026).

Bug original: o codigo pegava os 2 primeiros digitos como DDD cegamente.
Cliente que digitou com codigo de operadora ('015 11 96421-8592') gerou
+5501511964218592 (16 digitos) e o Pagar.me RECUSOU a cobranca inteira
(venda de R$426 perdida). Telefone e OPCIONAL: ou normaliza pra um
(DDD, numero) valido, ou retorna None pro caller OMITIR — NUNCA derruba
a cobranca.
"""
import pytest

from app.services.pagarme import _telefone_br


@pytest.mark.parametrize('entrada,esperado', [
    # movel local (11 digitos)
    ('11964218592', ('11', '964218592')),
    ('(11) 96421-8592', ('11', '964218592')),
    # com codigo de pais 55 (13 digitos)
    ('5511964218592', ('11', '964218592')),
    ('+55 11 96421-8592', ('11', '964218592')),
    # fixo local (10 digitos)
    ('1136421859', ('11', '36421859')),
    # tronco '0' simples (12 digitos)
    ('011964218592', ('11', '964218592')),
    # INCIDENTE 22/06/2026: codigo de operadora '015'
    ('01511964218592', ('11', '964218592')),
    ('0 15 11 96421-8592', ('11', '964218592')),
    # DDD 55 (Santa Maria/RS) preservado quando NAO ha codigo de pais
    ('55964218592', ('55', '964218592')),
    # DDD 55 COM codigo de pais 55
    ('5555964218592', ('55', '964218592')),
])
def test_telefone_valido(entrada, esperado):
    assert _telefone_br(entrada) == esperado


@pytest.mark.parametrize('entrada', [
    '', None, '123', '999', '9999999999999999999',
])
def test_telefone_invalido_retorna_none(entrada):
    # None = caller OMITE o telefone; a cobranca NUNCA pode falhar por isso.
    assert _telefone_br(entrada) is None


def test_telefone_montado_nunca_estoura_15_digitos():
    """+55 + area_code + number tem que caber no limite do Pagar.me (15)."""
    for entrada in ('11964218592', '5511964218592', '01511964218592',
                    '1136421859', '55964218592'):
        fone = _telefone_br(entrada)
        assert fone is not None
        ddd, num = fone
        montado = '55' + ddd + num  # o que o Pagar.me prefixa com '+'
        assert 7 <= len(montado) <= 15

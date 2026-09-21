"""Custo desconhecido é None; zero é um valor informado, nunca um substituto."""
from math import isfinite

from app.utils import parse_float_br


def parse_custo_opcional(raw):
    try:
        valor = parse_float_br(raw)
        if valor is None:
            return None
    except (TypeError, ValueError) as exc:
        raise ValueError('Informe um custo válido ou deixe em branco para preencher depois.') from exc
    if not isfinite(valor) or valor < 0:
        raise ValueError('O custo deve ser zero ou positivo, ou ficar em branco.')
    return valor


def somar_custos(valores):
    valores = list(valores)
    return None if any(v is None for v in valores) else sum(valores)

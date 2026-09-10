"""Comparação monetária usada nas decisões explícitas de cargo do RH."""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def valor_decimal(valor):
    """Valores da base legada em float são convertidos sem operações binárias."""
    try:
        numero = Decimal(str(valor if valor is not None else 0))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError('Revise os valores de remuneração antes de confirmar.') from None
    if not numero.is_finite() or numero < 0:
        raise ValueError('Revise os valores de remuneração antes de confirmar.')
    return numero


def reais(valor):
    try:
        return valor.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise ValueError('Revise os valores de remuneração antes de confirmar.') from None


def comparar(funcionario, cargo=None):
    """Prevê base + confiança + premiação, sem alterar nenhum cadastro.

    A ausência de destino significa manter o cargo e os valores vigentes.
    Confiança conserva 40% da base se já habilitada. Não inclui VT, VR,
    horas extras, descontos ou propostas remuneratórias da planilha.
    """
    base_atual = valor_decimal(funcionario.salario_efetivo())
    base_nova = valor_decimal(cargo.salario_base) if cargo else base_atual
    premio = valor_decimal(funcionario.premiacao)
    tem_confianca = bool(funcionario.tem_cargo_confianca)
    fator = Decimal('0.40') if tem_confianca else Decimal(0)
    confianca_atual, confianca_nova = base_atual * fator, base_nova * fator
    return {
        'salario_base_atual': reais(base_atual),
        'salario_base_novo': reais(base_nova),
        'premiacao': reais(premio),
        'tem_cargo_confianca': tem_confianca,
        'confianca_atual': reais(confianca_atual),
        'confianca_nova': reais(confianca_nova),
        'total_referencia_atual': reais(base_atual + confianca_atual + premio),
        'total_referencia_novo': reais(base_nova + confianca_nova + premio),
    }

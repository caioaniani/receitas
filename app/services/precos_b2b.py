"""Condições e subtotal B2B: desconto sobre a linha, nunca sobre a unidade arredondada."""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def normalizar_desconto(bruto):
    """Percentual informado no formulário; ausência é zero, inválido é erro."""
    if bruto is None or str(bruto).strip() == '':
        return Decimal('0')
    try:
        valor = Decimal(str(bruto).strip().replace(',', '.'))
    except (InvalidOperation, ValueError):
        raise ValueError('Desconto por item inválido: informe um percentual de 0 a 100.') from None
    if not valor.is_finite() or not 0 <= valor <= 100:
        raise ValueError('Desconto por item inválido: informe um percentual de 0 a 100.')
    return valor


def condicoes_sugeridas(atacado, desconto=0, especifico=None):
    """Preço base e percentual separados. A tabela do cliente já é final."""
    if especifico is not None:
        preco, percentual = Decimal(str(especifico)), Decimal('0')
    elif atacado:
        preco, percentual = Decimal(str(atacado)), Decimal(str(desconto or 0))
    else:
        return None
    return {'preco_unitario': format(preco.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), 'f'),
            'desconto_percentual': format(percentual, 'f')}


def subtotal_com_desconto(quantidade, preco_unitario, desconto_percentual=0):
    """Centavos exatos, arredondados uma vez depois de aplicar o desconto."""
    bruto = Decimal(str(quantidade or 0)) * Decimal(str(preco_unitario or 0))
    desconto = Decimal(str(desconto_percentual or 0))
    return (bruto * (1 - desconto / 100)).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP)

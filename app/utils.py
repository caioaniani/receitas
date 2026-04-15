"""Utilitários compartilhados."""


def parse_float_br(value, default=None):
    """Converte string com formato brasileiro (vírgula) para float.

    >>> parse_float_br('1.234,56')
    1234.56
    >>> parse_float_br('', default=0)
    0
    """
    if not value:
        return default
    cleaned = value.replace(',', '.').strip()
    return float(cleaned) if cleaned else default

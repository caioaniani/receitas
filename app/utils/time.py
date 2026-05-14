"""Helpers de timezone — sistema todo em BRT (America/Sao_Paulo).

Brasil nao tem DST desde 2019, entao offset fixo -3 e estavel.
Todas as colunas DateTime do banco armazenam BRT naive (sem tzinfo).
"""

from datetime import date, datetime, timedelta, timezone

BRT = timezone(timedelta(hours=-3))


def agora() -> datetime:
    """datetime atual em BRT, naive — default de colunas DateTime."""
    return datetime.now(BRT).replace(tzinfo=None)


def hoje() -> date:
    """date atual em BRT."""
    return datetime.now(BRT).date()


def para_brt(dt: datetime) -> datetime:
    """Converte um datetime (aware ou naive-UTC legado) pra BRT naive.

    Usar so quando le dado antigo possivelmente em UTC. Dados novos
    ja sao escritos em BRT pelo `agora()`.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BRT).replace(tzinfo=None)

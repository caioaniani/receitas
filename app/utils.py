"""Utilitários compartilhados."""
from datetime import date, datetime, timedelta, timezone


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


# ── Timezone helpers (BRT / America/Sao_Paulo) ─────────────────────────
# Sistema todo opera em BRT naive. Brasil nao tem DST desde 2019, offset -3 fixo.

BRT = timezone(timedelta(hours=-3))


def agora() -> datetime:
    """datetime atual em BRT, naive — default de colunas DateTime."""
    return datetime.now(BRT).replace(tzinfo=None)


def hoje() -> date:
    """date atual em BRT."""
    return datetime.now(BRT).date()


def para_brt(dt):
    """Converte um datetime (aware ou naive-UTC legado) pra BRT naive.

    Usar so quando le dado antigo possivelmente em UTC. Dados novos
    ja sao escritos em BRT pelo `agora()`.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BRT).replace(tzinfo=None)


def resolver_loja_por_nome(nome, *, somente_ativas=False):
    """Fuzzy match de Loja por nome: case-insensitive exata, depois ilike.

    Retorna Loja ou None. Centraliza o padrao que estava duplicado em
    varios services (copilot, etc).
    """
    from sqlalchemy import func
    from app.models import Loja
    nome = (nome or '').strip()
    if not nome:
        return None
    q_base = Loja.query
    if somente_ativas:
        q_base = q_base.filter(Loja.ativa.is_(True))
    loja = q_base.filter(func.lower(Loja.nome) == nome.lower()).first()
    if loja:
        return loja
    return q_base.filter(Loja.nome.ilike(f'%{nome}%')).first()

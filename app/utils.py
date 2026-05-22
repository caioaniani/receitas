"""Utilitários compartilhados."""
from datetime import UTC, date, datetime, timedelta, timezone


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
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(BRT).replace(tzinfo=None)


def comprimir_imagem(file_bytes, *, max_size=700, quality=82):
    """Comprime imagem via PIL: resize proporcional + JPEG quality.

    Aplica EXIF transpose (corrige rotacao de iPhone/Android) e converte
    pra RGB (descarta canal alpha de PNG). Resultado: JPEG progressivo
    otimizado.

    - `max_size`: maior lado (proporcional). Default 700.
    - `quality`: 1-95. Default 82 (~50-150KB pra fotos comuns).

    Retorna `bytes` (sempre JPEG). Levanta `ValueError` se file_bytes
    vazio ou imagem invalida.
    """
    import io

    from PIL import Image, ImageOps

    if not file_bytes:
        raise ValueError('Arquivo vazio')

    img = Image.open(io.BytesIO(file_bytes))
    img = ImageOps.exif_transpose(img)
    if img.mode in ('RGBA', 'P', 'LA'):
        img = img.convert('RGB')
    img.thumbnail((max_size, max_size), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format='JPEG', quality=quality, optimize=True, progressive=True)
    return out.getvalue()


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

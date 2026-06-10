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


def normalizar_telefone(numero):
    """Mantem so digitos. '+55 11 99999-9999' -> '5511999999999'."""
    return ''.join(c for c in (numero or '') if c.isdigit())


def telefone_chave(numero):
    """Chave canonica pra casar telefones brasileiros salvos em formatos
    diferentes (com/sem +55, com/sem o 9o digito de celular).

    O WhatsApp entrega '5511999998888' (13 digitos). Um PedidoLocal pode
    ter '(11) 99999-8888' -> '11999998888' (11) ou o formato antigo sem o
    9 -> '1199998888' (10). Esta funcao colapsa os tres no mesmo valor de
    10 digitos (DDD + 8 digitos do assinante), pra comparacao confiavel.

    Retorna '' se nao houver digitos suficientes pra um match seguro
    (< 10 digitos = sem DDD; nao tentamos adivinhar).
    """
    d = normalizar_telefone(numero)
    # Tira codigo do pais (55) quando presente em numero longo.
    if len(d) >= 12 and d.startswith('55'):
        d = d[2:]
    # Celular com 9o digito (DD 9 XXXXXXXX = 11 digitos): remove o 9 pra
    # bater com cadastros antigos sem ele.
    if len(d) == 11 and d[2] == '9':
        d = d[:2] + d[3:]
    if len(d) < 10:
        return ''
    # Mantem os ultimos 10 digitos (DDD + 8). Descarta eventual lixo a mais.
    return d[-10:]


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

    # PIL levanta UnidentifiedImageError/OSError (NAO ValueError) pra formatos
    # nao suportados (ex: HEIC do iPhone). Converte tudo em ValueError pra quem
    # chama (salvar_foto) tratar como erro de imagem e devolver JSON, nunca 500.
    try:
        img = Image.open(io.BytesIO(file_bytes))
        img = ImageOps.exif_transpose(img)
        if img.mode in ('RGBA', 'P', 'LA'):
            img = img.convert('RGB')
        img.thumbnail((max_size, max_size), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format='JPEG', quality=quality, optimize=True, progressive=True)
        return out.getvalue()
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValueError(f'imagem invalida ou formato nao suportado ({e})') from e


def normalizar_busca(s):
    """Normaliza string para busca: acento-insensível, case-insensitive.

    >>> normalizar_busca('Pão Francês')
    'pao frances'
    """
    import unicodedata
    s = unicodedata.normalize('NFKD', s or '')
    return ''.join(c for c in s if not unicodedata.combining(c)).lower()


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


def dividir_etapas_preparo(texto):
    """Divide um modo de preparo em etapas: blocos separados por linha em
    branco. Normaliza \r\n (textarea manda CRLF). Texto corrido (sem linha em
    branco) vira 1 etapa unica. Usado pela ficha (editor modular) e pela
    visao do padeiro (cards numerados)."""
    import re
    texto = (texto or '').replace('\r\n', '\n').replace('\r', '\n')
    return [e.strip() for e in re.split(r'\n\s*\n', texto) if e.strip()]

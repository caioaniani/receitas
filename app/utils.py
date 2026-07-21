"""Utilitários compartilhados."""
from datetime import UTC, date, datetime, timedelta, timezone


def parse_float_br(value, default=None):
    """Converte string com formato brasileiro para float.

    A vírgula é o separador decimal; o ponto é separador de milhar e só é
    removido quando há vírgula na string (senão um '.' isolado é decimal,
    ex.: '3.5'). Assim '1.234,56' -> 1234.56 sem quebrar '3,5' nem '3.5'.
    Vazio/None -> `default`; valor presente porém inválido levanta ValueError
    (nunca vira o default silenciosamente — dinheiro/estoque não podem virar
    zero calado; quem chama traduz o erro em 400/flash se precisar).

    >>> parse_float_br('1.234,56')
    1234.56
    >>> parse_float_br('3,5')
    3.5
    >>> parse_float_br('3.5')
    3.5
    >>> parse_float_br('', default=0)
    0
    """
    if value is None:
        return default
    cleaned = str(value).strip()
    if not cleaned:
        return default
    if ',' in cleaned:
        cleaned = cleaned.replace('.', '').replace(',', '.')
    return float(cleaned)


def parse_fator_composicao(raw, default=1.0):
    """Fator de composição do PDV: quantas unidades do alvo 1 venda consome.

    Aceita vírgula PT-BR ("0,2" == 0.2). Vazio/None -> `default`. Valor
    PRESENTE porém inválido (não-número) ou <= 0 -> ValueError.

    Regra de estoque (CLAUDE.md): NUNCA cair pra 1.0 em silêncio. Um fator
    digitado errado que vira 1.0 baixaria estoque errado — ex.: 0,2 (composto,
    5 vendas = 1 inteiro) virando 1,0 baixaria 5x. Quem chama traduz o
    ValueError em 400/flash, em vez de inventar um valor.

    >>> parse_fator_composicao('0,2')
    0.2
    >>> parse_fator_composicao('')      # vazio -> default
    1.0
    >>> parse_fator_composicao(None)
    1.0
    """
    if raw is None:
        return default
    s = str(raw).strip().replace(',', '.')
    if s == '':
        return default
    f = float(s)  # ValueError se não for número — propaga de propósito
    if f <= 0:
        raise ValueError(f'fator deve ser > 0 (recebido {raw!r})')
    return f


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


def fmt_brl(v, centavos=True):
    """Dinheiro em pt-BR: 1135.5 -> 'R$ 1.135,50' (ou 'R$ 1.136' sem
    centavos). Centralizado (18/07/2026) — mensagem de WhatsApp/alerta com
    formato americano ('R$ 1,135.00') convidava leitura errada de valor."""
    try:
        n = float(v or 0)
    except (TypeError, ValueError):
        n = 0.0
    s = f'{n:,.2f}' if centavos else f'{round(n):,.0f}'
    return 'R$ ' + s.replace(',', '\x00').replace('.', ',').replace('\x00', '.')


def hosts_loja():
    """Conjunto de hosts (lower, sem porta) que servem a LOJA pública —
    config `LOJA_HOSTS` (default 'opao.online,www.opao.online').

    Centraliza o parsing pra a lista do roteamento por host
    (`_roteamento_por_host`), do gate da loja e do header noindex serem a
    MESMA — divergir aqui geraria loja pública num host e privada noutro."""
    from flask import current_app
    return {h.strip().lower()
            for h in (current_app.config.get('LOJA_HOSTS') or '').split(',')
            if h.strip()}


def host_atual_eh_loja():
    """True se o host da request atual é um domínio público da loja
    (ex: opao.online). Em gestao.*/railway.app/outros é False — lá a loja
    só responde pra admin logado e fica fora do Google (anti-duplicação).

    Fallback defensivo: se `LOJA_HOSTS` estiver vazio, devolve True (não
    barra acesso) — melhor não quebrar o site público do que travar tudo."""
    from flask import request
    hosts = hosts_loja()
    if not hosts:
        return True
    host = (request.host or '').split(':')[0].lower()
    return host in hosts



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


# Tipos de ingrediente de receita que são SUB-RECEITA (consomem outra receita).
# 'receita' = quantidade absoluta de unidades; 'sub_pct' = % da base (como MP %).
SUB_RECEITA_TIPOS = ('receita', 'sub_pct')


def unidades_subreceita(tipo, porcentagem, peso_base):
    """Unidades-BASE de uma SUB-RECEITA consumidas por 1 fornada-base do pai.

    Fonte ÚNICA dos dois modos (13/07/2026) pra NENHUM motor (compra, baixa de
    produção, custo, cronograma, pré-preparo) divergir:

      - `receita`  → QUANTIDADE ABSOLUTA de unidades da sub: `porcentagem`.
      - `sub_pct`  → % da base, IGUAL a MP %: `porcentagem/100 * peso_base`.

    Cada motor multiplica o retorno pelo seu multiplicador/ratio próprio
    (`unidades_pai / rendimento`). Tipo desconhecido cai no absoluto (compat —
    receita antiga não muda de comportamento)."""
    pct = porcentagem or 0
    if tipo == 'sub_pct':
        return pct / 100.0 * (peso_base or 0)
    return pct


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


def comprimir_logo(file_bytes, *, max_size=520):
    """Processa um LOGO preservando transparencia (PNG continua PNG).

    Diferente de `comprimir_imagem`, que forca JPEG e descarta o canal
    alpha — ruim pra logo sobre fundo claro (apareceria caixa branca).
    SVG passa direto (vetor). Raster com alpha vira PNG; sem alpha vira
    JPEG (menor).

    Retorna (bytes, mimetype, extensao). Levanta ValueError se invalido.
    """
    import io

    if not file_bytes:
        raise ValueError('Arquivo vazio')

    # SVG: vetor — sobe como veio (escala perfeita em qualquer tamanho).
    cabeca = file_bytes[:512].lstrip().lower()
    if cabeca.startswith(b'<?xml') or b'<svg' in cabeca[:200]:
        return file_bytes, 'image/svg+xml', 'svg'

    from PIL import Image, ImageOps
    try:
        img = Image.open(io.BytesIO(file_bytes))
        img = ImageOps.exif_transpose(img)
        tem_alpha = img.mode in ('RGBA', 'LA', 'P')
        img.thumbnail((max_size, max_size), Image.LANCZOS)
        out = io.BytesIO()
        if tem_alpha:
            img = img.convert('RGBA')
            img.save(out, format='PNG', optimize=True)
            return out.getvalue(), 'image/png', 'png'
        img = img.convert('RGB')
        img.save(out, format='JPEG', quality=88, optimize=True, progressive=True)
        return out.getvalue(), 'image/jpeg', 'jpg'
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

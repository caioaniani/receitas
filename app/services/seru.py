"""Cliente da API Seru (PDV).

Doc: https://integration.plataformaseru.com.br/v1/docs

Autenticação OAuth2 client_credentials. Token tem expiração; cachemos em
memória do processo (com pequeno safety margin) e renovamos sob demanda.
"""
import base64
import logging
import time
from datetime import UTC, datetime, timedelta

import requests
from flask import current_app

from app.utils import BRT

logger = logging.getLogger(__name__)

BASE = 'https://integration.plataformaseru.com.br/v1'


def data_local(iso_utc):
    """Converte string ISO UTC ('2026-05-07T01:30:00Z') pra date em
    horario de Sao Paulo (BRT). Retorna None se input invalido.

    Crucial: a Seru devolve createdAt/updatedAt em UTC. Sem conversao,
    pedidos feitos depois das 21h BRT caem no dia seguinte UTC e sao
    filtrados incorretamente.
    """
    if not iso_utc:
        return None
    try:
        s = iso_utc.replace('Z', '+00:00')
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(BRT).date()
    except (ValueError, TypeError):
        return None


def datahora_local(iso_utc):
    """Como `data_local`, mas devolve o datetime COMPLETO em BRT (naive) —
    pra quando a HORA da venda importa (ex: auditoria de cobrança)."""
    if not iso_utc:
        return None
    try:
        s = iso_utc.replace('Z', '+00:00')
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(BRT).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


# Cache do token entre requests do mesmo worker. Se expira, renovamos.
_token_cache = {'access_token': None, 'expires_at': 0}


def _credenciais():
    cid = (current_app.config.get('SERU_CLIENT_ID') or '').strip()
    secret = (current_app.config.get('SERU_CLIENT_SECRET') or '').strip()
    return cid, secret


def _obter_token(force_refresh=False):
    """Retorna access token valido, renovando quando necessario."""
    cid, secret = _credenciais()
    if not cid or not secret:
        raise RuntimeError('SERU_CLIENT_ID/SERU_CLIENT_SECRET nao configurados.')

    agora = time.time()
    if not force_refresh and _token_cache['access_token'] and agora < _token_cache['expires_at'] - 30:
        return _token_cache['access_token']

    basic = base64.b64encode(f'{cid}:{secret}'.encode()).decode()
    r = requests.post(
        f'{BASE}/oauth/token',
        headers={'Authorization': f'Basic {basic}', 'Content-Type': 'application/json'},
        json={'grantType': 'client_credentials'},
        timeout=20,
    )
    if r.status_code not in (200, 201):
        logger.error('Seru auth falhou %s: %s', r.status_code, r.text[:300])
        raise RuntimeError(f'Seru auth {r.status_code}: {r.text[:200]}')
    body = r.json()
    _token_cache['access_token'] = body.get('accessToken')
    expires_in = int(body.get('expiresIn') or 3600)
    _token_cache['expires_at'] = agora + expires_in
    return _token_cache['access_token']


# Tentativas EXTRAS em falha transitoria: rede (SSL EOF, conexao derrubada,
# timeout) e gateway 5xx (502/503/504 — GET idempotente). Outros erros HTTP
# (4xx, 500 de aplicacao) nao re-tentam.
_RETRIES_REDE = 2


class _Erro5xx(RuntimeError):
    """502/503/504 do gateway do Seru — transitorio, re-tentavel."""


def _get_uma_vez(path, params=None):
    """GET autenticado (uma tentativa). Renova token automaticamente em 401."""
    token = _obter_token()
    r = requests.get(f'{BASE}{path}',
                     headers={'Authorization': f'Bearer {token}'},
                     params=params or {}, timeout=20)
    if r.status_code == 401:
        token = _obter_token(force_refresh=True)
        r = requests.get(f'{BASE}{path}',
                         headers={'Authorization': f'Bearer {token}'},
                         params=params or {}, timeout=20)
    if r.status_code != 200:
        logger.error('Seru %s %s: %s', path, r.status_code, r.text[:300])
        if r.status_code in (502, 503, 504):
            # Gateway/indisponibilidade transitoria do lado deles (Sentry
            # 13/07/2026: 502 no /orders) — re-tentavel como falha de rede.
            raise _Erro5xx(f'Seru {path} {r.status_code}: {r.text[:200]}')
        raise RuntimeError(f'Seru {path} {r.status_code}: {r.text[:200]}')
    return r.json()


def _get(path, params=None):
    """GET autenticado com retry de REDE: a API do Seru derruba conexoes
    sob carga (Sentry 12/07/2026: SSLEOFError no handshake, pagina 3 do
    fetch paralelo) e uma unica queda abortava a captura inteira do dia.
    Ate 2 novas tentativas com backoff curto (0,5s / 1,5s) — GET e
    idempotente. SSLError e subclasse de ConnectionError no requests."""
    ultimo = None
    for tentativa in range(_RETRIES_REDE + 1):
        if tentativa:
            time.sleep(0.5 * (3 ** (tentativa - 1)))
        try:
            return _get_uma_vez(path, params)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout, _Erro5xx) as e:
            ultimo = e
            logger.warning('Seru %s: falha de rede (tentativa %d/%d): %s',
                           path, tentativa + 1, _RETRIES_REDE + 1,
                           str(e)[:200])
    raise ultimo


def _iso_dia(data, fim=False):
    """Converte uma date BRT pra ISO 8601 UTC.

    Inicio do dia BRT (00:00 -3) = 03:00 UTC do mesmo dia.
    Fim do dia BRT (23:59:59 -3) = 02:59:59 UTC do dia seguinte.
    """
    if fim:
        dt_brt = datetime.combine(data, datetime.max.time().replace(microsecond=0), tzinfo=BRT)
    else:
        dt_brt = datetime.combine(data, datetime.min.time(), tzinfo=BRT)
    return dt_brt.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def listar_pedidos(data_inicial, data_final, page=1, limit=100, hasCanceledItem=None,
                   initial_updated_at=None, final_updated_at=None):
    """Lista pedidos da Seru. Por padrao filtra por updatedAt no intervalo
    [data_inicial, data_final], mas o caller pode sobrescrever passando
    initial_updated_at/final_updated_at (objetos date).

    Retorna dict {success, page, limit, totalPages, data: [...]}
    """
    iu = initial_updated_at or data_inicial
    fu = final_updated_at or data_final
    params = {
        'initialUpdatedAt': _iso_dia(iu, fim=False),
        'finalUpdatedAt': _iso_dia(fu, fim=True),
        'page': page,
        'limit': limit,
    }
    if hasCanceledItem is not None:
        params['hasCanceledItem'] = 'true' if hasCanceledItem else 'false'
    return _get('/orders', params=params)


def listar_pedidos_completo(data_inicial, data_final, expandir_dias_frente=0, debug=None):
    """Itera todas as páginas e devolve uma lista única.

    A Seru limita cada chamada a uma janela de 24h em updatedAt. Pra cobrir
    intervalos maiores ou pegar pedidos atualizados depois, fazemos uma chamada
    POR DIA desde data_inicial ate data_final + expandir_dias_frente.
    Chamadas executam em paralelo (threads) pra reduzir latencia.

    Caller deve filtrar pelo createdAt depois pra precisao.
    """
    from concurrent.futures import ThreadPoolExecutor

    from flask import current_app
    app = current_app._get_current_object()

    fim_busca = data_final + timedelta(days=expandir_dias_frente)
    dias = []
    d = data_inicial
    while d <= fim_busca:
        dias.append(d)
        d += timedelta(days=1)
        if len(dias) > 60:  # safety
            logger.warning('Seru listar_pedidos: parando em 60 dias')
            break

    def _fetch(dia, page):
        with app.app_context():
            return dia, page, listar_pedidos(dia, dia, page=page, limit=100)

    todos = []
    # Etapa 1: pega pagina 1 de cada dia em paralelo
    with ThreadPoolExecutor(max_workers=6) as ex:
        firsts = list(ex.map(lambda d: _fetch(d, 1), dias))

    # Coleta dados + identifica paginas extras
    extras = []
    for dia, page, r in firsts:
        data_pagina = r.get('data') or []
        todos.extend(data_pagina)
        total = r.get('totalPages') or 1
        if debug is not None:
            debug.append({
                'dia': dia.isoformat(), 'page': page,
                'qtd_recebida': len(data_pagina),
                'totalPages': total,
                'success': r.get('success'),
            })
        for p in range(2, min(total + 1, 51)):
            extras.append((dia, p))

    # Etapa 2: paginas extras em paralelo
    if extras:
        with ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(lambda x: _fetch(x[0], x[1]), extras))
        for dia, page, r in results:
            data_pagina = r.get('data') or []
            todos.extend(data_pagina)
            if debug is not None:
                debug.append({
                    'dia': dia.isoformat(), 'page': page,
                    'qtd_recebida': len(data_pagina),
                    'totalPages': r.get('totalPages') or 1,
                    'success': r.get('success'),
                })

    return todos


def detalhes_pedido(pedido_id):
    return _get(f'/orders/{pedido_id}')


_NF_XML_MAX_BYTES = 3_000_000
_NF_STATUS_INVALIDOS = ('canceled', 'cancelled', 'denied', 'rejected', 'error')


def itens_da_nf(pedido, timeout=12):
    """Produtos REAIS de um pedido SEM itens, extraídos do XML da NFC-e.

    Caso 99Food (18/07/2026): a integração de delivery manda o pedido ao
    Seru só com o TOTAL — nem a listagem nem o GET /orders/{id} trazem os
    produtos. Mas a NFC-e emitida (taxInvoice.xmlUrl, S3 da Seru) lista
    tudo (<det><prod><xProd>/qCom/vProd), com os MESMOS nomes do
    SeruProdutoMap — provado no pedido 3377f6c3/NF 724. O sync usa isto
    pra dar baixa de estoque (pedido do dono).

    Retorna:
      []    — pedido sem NF utilizável (sem taxInvoice/URL, ou NF
              cancelada/negada): nada a enriquecer, segue o fluxo normal;
      list  — itens na MESMA forma de `extrair_itens`;
      None  — NF existe mas o download/parse FALHOU: o chamador NÃO deve
              marcar o pedido como processado (retenta no próximo ciclo).
    """
    import xml.etree.ElementTree as ET

    ti = pedido.get('taxInvoice') if isinstance(pedido, dict) else None
    if not isinstance(ti, dict):
        return []
    if str(ti.get('status') or '').lower() in _NF_STATUS_INVALIDOS:
        return []
    xml_txt = (ti.get('xml') or '').strip()
    url = (ti.get('xmlUrl') or '').strip()
    if not xml_txt and not url:
        return []
    try:
        # Parse em BYTES quando baixado: o expat honra a declaração de
        # encoding do XML (decode 'replace' corromperia acento de nome de
        # produto e criaria mapeamento pendente duplicado).
        xml_src = xml_txt.encode('utf-8') if xml_txt else None
        if xml_src is None:
            r = requests.get(url, timeout=timeout)
            if r.status_code != 200:
                logger.warning('itens_da_nf: xmlUrl devolveu HTTP %s '
                               '(pedido %s)', r.status_code, pedido.get('id'))
                return None
            if len(r.content) > _NF_XML_MAX_BYTES:
                logger.warning('itens_da_nf: XML anômalo (%s bytes) — '
                               'pedido %s', len(r.content), pedido.get('id'))
                return None
            xml_src = r.content

        def _local(tag):
            return tag.split('}')[-1]

        def _num(v):
            try:
                return float(str(v).replace(',', '.'))
            except (TypeError, ValueError):
                return 0.0

        root = ET.fromstring(xml_txt)
        itens = []
        for det in root.iter():
            if _local(det.tag) != 'det':
                continue
            prod = next((c for c in det if _local(c.tag) == 'prod'), None)
            if prod is None:
                continue
            campos = {_local(c.tag): (c.text or '').strip() for c in prod}
            nome = campos.get('xProd') or ''
            if not nome:
                continue
            qtd = _num(campos.get('qCom')) or 1.0
            itens.append({
                'nome': nome,
                'sku': campos.get('cProd') or None,
                'qtd': qtd,
                'preco_unit': _num(campos.get('vUnCom')),
                'total': _num(campos.get('vProd')),
                'cancelado': False,
            })
        return itens
    except Exception:  # noqa: BLE001 — falha vira retentativa, nunca crash
        logger.exception('itens_da_nf: download/parse falhou (pedido %s)',
                         pedido.get('id') if isinstance(pedido, dict) else '?')
        return None


def pedido_cancelado(pedido):
    """Pedido cancelado — por `canceledAt` OU por `status == 'canceled'`.

    Caso real 18/07/2026 (Nebraska, cód 19797307): cobrança cancelada veio
    com status 'canceled' mas canceledAt VAZIO — só olhar canceledAt a
    contava como venda no snapshot/relatórios e no vigia de venda sem item.
    Camada de RELATÓRIO/vigia usa este helper; o seru_sync (estoque) segue
    keyed em canceledAt de propósito — mudar o gatilho de estorno é decisão
    separada (estoque tem peso especial)."""
    if not isinstance(pedido, dict):
        return False
    if pedido.get('canceledAt'):
        return True
    return str(pedido.get('status') or '').strip().lower() == 'canceled'


def canal_tag(pedido):
    """Tag do canal de venda ('pdv-facil', '99food', ...) ou ''."""
    sc = pedido.get('salesChannel') if isinstance(pedido, dict) else None
    if isinstance(sc, dict):
        return str(sc.get('tag') or sc.get('code') or '').strip().lower()
    return ''


def extrair_itens(pedido):
    """Normaliza a lista de itens de um pedido Seru.

    A Seru pode usar 'items', 'orderItems' ou 'products' pra lista, e cada
    item pode ter o nome em 'name', 'productName', 'product.name' etc.
    Tentamos varios campos pra ser robusto a variacoes da API/conta.

    Retorna [{nome, sku, qtd, preco_unit, total, cancelado}].
    """
    if not isinstance(pedido, dict):
        return []
    itens_raw = (pedido.get('items') or pedido.get('orderItems')
                 or pedido.get('products') or [])
    if not isinstance(itens_raw, list):
        return []

    def _f(v, default=0.0):
        try:
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    def _s(v):
        if v is None:
            return ''
        if isinstance(v, str):
            return v.strip()
        if isinstance(v, dict):
            return str(v.get('name') or v.get('label') or v.get('description')
                       or v.get('sku') or v.get('code') or '').strip()
        return str(v).strip()

    out = []
    for it in itens_raw:
        if not isinstance(it, dict):
            continue
        # Nome — tenta varios campos
        nome = (_s(it.get('name')) or _s(it.get('productName'))
                or _s(it.get('product')) or _s(it.get('description'))
                or _s(it.get('title')))
        if not nome:
            continue
        sku = (_s(it.get('sku')) or _s(it.get('code'))
               or _s(it.get('productCode')) or _s(it.get('barcode')))
        qtd = _f(it.get('quantity') or it.get('qty') or it.get('amount'), 0.0)
        if qtd <= 0:
            continue
        preco_unit = _f(it.get('unitPrice') or it.get('price')
                        or it.get('value'), 0.0)
        total = _f(it.get('total') or it.get('subtotal')
                   or it.get('totalPrice'), preco_unit * qtd)
        cancelado = bool(it.get('canceledAt') or it.get('canceled')
                         or it.get('status') == 'canceled')
        out.append({
            'nome': nome, 'sku': sku, 'qtd': qtd,
            'preco_unit': preco_unit, 'total': total,
            'cancelado': cancelado,
        })
    return out

"""Cliente da Google Maps Platform: Geocoding API + Directions API.

Geocoding: endereco -> lat/lng (cache permanente em GeocodeCache).
Directions: ordem otimizada de waypoints + distancia + tempo estimado.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import current_app

from app.extensions import db
from app.models import GeocodeCache

logger = logging.getLogger(__name__)


def _api_key():
    return (current_app.config.get('GOOGLE_MAPS_API_KEY') or '').strip()


def _normalizar_chave(endereco):
    if not endereco:
        return ''
    return ' '.join(endereco.strip().lower().split())[:200]


# ── Geocoding ──

def _geocode_remoto(endereco, key=None):
    """Chama Geocoding API. Retorna (lat, lng) ou None.

    `key` pode ser passada explicitamente quando esta em thread (sem app context).
    Senao, le do current_app."""
    if not endereco:
        return None
    if key is None:
        try:
            key = _api_key()
        except RuntimeError:
            # Fora de app context (thread sem contexto Flask) — chamador deve passar key
            return None
    if not key:
        return None
    try:
        r = requests.get(
            'https://maps.googleapis.com/maps/api/geocode/json',
            params={
                'address': endereco,
                'key': key,
                'components': 'country:BR',
                'language': 'pt-BR',
            },
            timeout=10,
        )
        if r.status_code != 200:
            logger.warning('Geocode http %s pra %r', r.status_code, endereco[:80])
            return None
        data = r.json()
        if data.get('status') != 'OK' or not data.get('results'):
            return None
        loc = data['results'][0]['geometry']['location']
        return float(loc['lat']), float(loc['lng'])
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        logger.warning('Geocode erro pra %r: %s', endereco[:80], e)
        return None


def _eh_fonte_confiavel(fonte):
    """Considera so Google como fonte confiavel. Resultados antigos (Nominatim,
    BrasilAPI, AwesomeAPI) eram instaveis — re-geocoda com Google."""
    return (fonte or '').startswith('google')


def geocode(endereco):
    """Retorna (lat, lng). Cacheia SO sucessos — falhas viram retry automatico
    na proxima chamada. Custo extra de re-bater enderecos invalidos cronicos
    (raro) e baixo e vale a robustez."""
    if not endereco:
        return None
    chave = _normalizar_chave(endereco)
    if not chave:
        return None

    # Hit valido (so confia em cache do Google com coords)
    cache = GeocodeCache.query.filter_by(chave=chave).first()
    if cache and cache.lat is not None and _eh_fonte_confiavel(cache.fonte):
        return cache.lat, cache.lng

    # Bate no Google (ignora qualquer cache antigo — re-tenta)
    coords = _geocode_remoto(endereco)
    if coords:
        if not cache:
            cache = GeocodeCache(chave=chave, fonte='google')
            db.session.add(cache)
        cache.lat, cache.lng = coords
        cache.fonte = 'google'
        db.session.commit()
        return coords

    # Falhou: deleta cache antigo (de qualquer fonte) pra forcar re-tentar
    # na proxima execucao. Nao persiste falha — UX simples, sem botoes manuais.
    if cache:
        db.session.delete(cache)
        db.session.commit()
    return None


# Resposta NEGATIVA do Google (ZERO_RESULTS) também fica em cache: sem isso o
# MESMO texto repetido (cliente clicando "Calcular frete" de novo, bot
# reenviando a consulta) gastava uma vaga do teto diário a cada vez (auditoria
# do checkout, 27/09/2026). Vale por um prazo — rua/condomínio novo passa a
# existir no Google — e o botão "limpar falhas" do painel de entregas
# (`api_google_limpar_falhas`, apaga lat NULL) também o descarta.
FONTE_ZERO = 'google_zero'
TTL_NEGATIVO_DIAS = 30


def tem_chave():
    """True se a chave do Google Maps está configurada (sem ela nenhuma
    chamada remota acontece — o frete não deve reservar vaga do teto)."""
    return bool(_api_key())


def _negativo_vigente(cache):
    from datetime import timedelta

    from app.utils import agora
    return (cache.criado_em is not None
            and agora() - cache.criado_em < timedelta(days=TTL_NEGATIVO_DIAS))


def cache_preciso(endereco, numero_entrega=None):
    """O que o cache do `geocode_preciso` já sabe, SEM chamar a API (custo
    zero). Fonte ÚNICA da regra de hit: o frete consulta antes de reservar
    vaga no teto diário e o `geocode_preciso` antes de bater no Google.

    Devolve:
      ('preciso', (lat, lng)) — hit que vale;
      ('negativo', None)      — o Google já respondeu que não serve: resultado
                                aproximado/parcial ('google_aprox') ou
                                ZERO_RESULTS dentro do prazo ('google_zero');
      (None, None)            — sem resposta útil em cache: consultar o Google.

    Com `numero_entrega` (despacho), só 'google_entrega' é hit: o cache
    'google' do frete não carrega a prova de porta/número."""
    chave = _normalizar_chave(endereco)
    if not chave:
        return None, None
    cache = GeocodeCache.query.filter_by(chave=chave).first()
    if not cache:
        return None, None
    if cache.fonte == FONTE_ZERO:
        return ('negativo', None) if _negativo_vigente(cache) else (None, None)
    if cache.lat is None:
        return None, None
    if cache.fonte == 'google_entrega' or (
            cache.fonte == 'google' and numero_entrega is None):
        return 'preciso', (cache.lat, cache.lng)
    if cache.fonte == 'google_aprox':
        return 'negativo', None
    return None, None


def _gravar_cache_preciso(chave, coords, fonte):
    """Grava a resposta do Google no cache. `criado_em` = momento DESTA
    resposta (é o relógio do prazo do cache negativo). Corrida com outro
    processo gravando a mesma chave (dois cliques em "Calcular frete") cai
    no unique: o savepoint desfaz só esta escrita e a sessão do chamador
    segue utilizável — sem isso a transação do checkout ficava inutilizada.
    As pendências do chamador vão ao banco ANTES (`flush`), fora do
    savepoint: erro delas é delas e sobe como antes, nunca vira "corrida"."""
    from sqlalchemy.exc import IntegrityError

    from app.utils import agora
    db.session.flush()
    try:
        with db.session.begin_nested():
            cache = GeocodeCache.query.filter_by(chave=chave).first()
            if not cache:
                cache = GeocodeCache(chave=chave)
                db.session.add(cache)
            cache.lat, cache.lng = coords if coords else (None, None)
            cache.fonte = fonte
            cache.criado_em = agora()
        db.session.commit()
    except IntegrityError:
        logger.info('geocode_preciso: outro processo gravou o cache de %r',
                    chave[:80])


def geocode_preciso(endereco, numero_entrega=None):
    """Como geocode(), mas devolve (lat, lng) SÓ quando o Google achou o
    ENDEREÇO de fato — `location_type` ROOFTOP/RANGE_INTERPOLATED/
    GEOMETRIC_CENTER e NÃO `partial_match`. Centroide de cidade (APPROXIMATE)
    ou match parcial → None, pra o frete cair na cadeia grátis (que tem os
    guards de homônimo) em vez de cobrar frete de um ponto aproximado como se
    fosse preciso (09/07/2026). Reusa o mesmo cache do geocode(): fonte
    'google' = preciso; 'google_aprox' = aproximado; 'google_zero' = o Google
    não achou nada (os dois últimos não re-batem a API — `cache_preciso`).
    Com numero_entrega, exige precisão de porta e número correspondente;
    só reutiliza cache 'google_entrega', validado com esses critérios."""
    if not endereco:
        return None
    chave = _normalizar_chave(endereco)
    if not chave:
        return None
    estado, coords = cache_preciso(endereco, numero_entrega)
    if estado == 'preciso':
        return coords
    if estado == 'negativo':
        return None
    key = _api_key()
    if not key:
        return None
    try:
        r = requests.get(
            'https://maps.googleapis.com/maps/api/geocode/json',
            params={'address': endereco, 'key': key,
                    'components': 'country:BR', 'language': 'pt-BR'},
            timeout=10)
        if r.status_code != 200:
            logger.warning('geocode_preciso http %s pra %r', r.status_code,
                           endereco[:80])
            return None
        data = r.json()
        if not isinstance(data, dict):
            logger.warning('geocode_preciso: resposta ilegível pra %r',
                           endereco[:80])
            return None
        status = data.get('status')
        if status == 'ZERO_RESULTS' or (status == 'OK'
                                        and not data.get('results')):
            # Resposta definitiva: o Google não conhece esse texto. Cacheia
            # pra não pagar de novo pelo mesmo texto (auditoria 27/09/2026).
            _gravar_cache_preciso(chave, None, FONTE_ZERO)
            return None
        if status != 'OK':
            # OVER_QUERY_LIMIT / REQUEST_DENIED / INVALID_REQUEST /
            # UNKNOWN_ERROR: transitório ou chave mal configurada — NÃO
            # cacheia (a próxima tentativa pode dar certo), mas avisa.
            logger.warning('geocode_preciso status %s (%s) pra %r', status,
                           (data.get('error_message') or '')[:200],
                           endereco[:80])
            return None
        res0 = (data.get('results') or [None])[0]
        if not isinstance(res0, dict):
            return None
        geom = res0.get('geometry') or {}
        loc = geom.get('location') or {}
        preciso = geom.get('location_type') in (
            'ROOFTOP', 'RANGE_INTERPOLATED', 'GEOMETRIC_CENTER') \
            and not res0.get('partial_match')
        if numero_entrega is not None:
            # Despacho exige rua/número, não centroide de CEP, rua ou cidade.
            # Cache antigo 'google' não carrega essa prova: revalida acima.
            numeros = {str(c.get('long_name', '')).strip()
                       for c in res0.get('address_components', [])
                       if 'street_number' in c.get('types', [])}
            preciso = (preciso
                       and geom.get('location_type') in ('ROOFTOP', 'RANGE_INTERPOLATED')
                       and bool(set(res0.get('types', [])) & {'street_address', 'premise', 'subpremise'})
                       and str(numero_entrega) in numeros)
        coords = (float(loc['lat']), float(loc['lng']))
        fonte = (('google_entrega' if numero_entrega is not None else 'google')
                 if preciso else 'google_aprox')
        _gravar_cache_preciso(chave, coords, fonte)
        return coords if preciso else None
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        logger.warning('geocode_preciso erro pra %r: %s', endereco[:80], e)
        return None


def geocode_em_lote(enderecos, max_workers=8):
    """Geocoda lista de enderecos em paralelo. Retorna dict {endereco: (lat, lng) | None}.
    Usa cache. Persiste no banco em batch (commits parciais)."""
    if not enderecos:
        return {}

    # So confia em cache hit do Google com coords. Qualquer outra coisa
    # (sem cache, fonte antiga, falha previa) → re-tenta no Google.
    resultados = {}
    pendentes = []
    for end in enderecos:
        if not end:
            resultados[end] = None
            continue
        chave = _normalizar_chave(end)
        cache = GeocodeCache.query.filter_by(chave=chave).first() if chave else None
        if cache and cache.lat is not None and _eh_fonte_confiavel(cache.fonte):
            resultados[end] = (cache.lat, cache.lng)
        else:
            pendentes.append(end)

    if not pendentes:
        return resultados

    # Captura a API key NO main thread (current_app nao existe em threads novas).
    key = _api_key()
    if not key:
        for end in pendentes:
            resultados[end] = None
        return resultados

    # Pra os pendentes, paraleliza — passa key explicitamente
    novos = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_geocode_remoto, e, key): e for e in pendentes}
        for f in as_completed(futures):
            e = futures[f]
            try:
                novos[e] = f.result()
            except Exception:
                novos[e] = None

    # Persiste sucessos. Falhas: deleta cache antigo (forca retry proxima vez).
    for e, coords in novos.items():
        chave = _normalizar_chave(e)
        if not chave:
            continue
        cache = GeocodeCache.query.filter_by(chave=chave).first()
        if coords:
            if not cache:
                cache = GeocodeCache(chave=chave, fonte='google')
                db.session.add(cache)
            cache.lat, cache.lng = coords
            cache.fonte = 'google'
        elif cache:
            db.session.delete(cache)
        resultados[e] = coords
    db.session.commit()
    return resultados


# ── Directions ──

def directions_otimizado(origem_latlng, paradas_latlng, retorno_origem=True):
    """origem_latlng: (lat, lng) da matriz.
    paradas_latlng: lista de (lat, lng) das paradas.
    Retorna {'ordem': [indices reordenados], 'km': float, 'minutos': int} ou None.

    Limites Directions: 25 waypoints alem de origin/destination.
    """
    key = _api_key()
    if not key or not paradas_latlng:
        return None
    if len(paradas_latlng) > 25:
        # Acima de 25, devolve sem otimizar (driver tem que dividir manualmente).
        # Ainda calcula distancia total estimada chamando em chunks? — fica pra fase 2.
        return None

    origin = f'{origem_latlng[0]},{origem_latlng[1]}'
    if retorno_origem:
        destination = origin
        waypoints = paradas_latlng
    else:
        destination = f'{paradas_latlng[-1][0]},{paradas_latlng[-1][1]}'
        waypoints = paradas_latlng[:-1]

    waypoints_str = 'optimize:true|' + '|'.join(f'{p[0]},{p[1]}' for p in waypoints)

    try:
        r = requests.get(
            'https://maps.googleapis.com/maps/api/directions/json',
            params={
                'origin': origin,
                'destination': destination,
                'waypoints': waypoints_str,
                'mode': 'driving',
                'language': 'pt-BR',
                'key': key,
            },
            timeout=15,
        )
        if r.status_code != 200:
            logger.warning('Directions http %s', r.status_code)
            return None
        data = r.json()
        if data.get('status') != 'OK' or not data.get('routes'):
            logger.warning('Directions status %s: %s', data.get('status'), data.get('error_message', ''))
            return None
        rota = data['routes'][0]
        # waypoint_order indica nova ordem (do array original de waypoints)
        ordem = rota.get('waypoint_order') or list(range(len(waypoints)))
        # Soma distancia/tempo de todas as legs
        km = sum(leg['distance']['value'] for leg in rota.get('legs', [])) / 1000.0
        seg = sum(leg['duration']['value'] for leg in rota.get('legs', []))
        return {'ordem': ordem, 'km': round(km, 1), 'minutos': round(seg / 60)}
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        logger.warning('Directions erro: %s', e)
        return None

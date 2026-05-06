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

def _geocode_remoto(endereco):
    """Chama Geocoding API. Retorna (lat, lng) ou None."""
    key = _api_key()
    if not key or not endereco:
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
    """Retorna (lat, lng) com cache. None se falhou."""
    if not endereco:
        return None
    chave = _normalizar_chave(endereco)
    if not chave:
        return None
    cache = GeocodeCache.query.filter_by(chave=chave).first()

    # Hit valido: cache do Google com coords
    if cache and cache.lat is not None and _eh_fonte_confiavel(cache.fonte):
        return cache.lat, cache.lng
    # Falha confirmada do Google: nao re-bate
    if cache and cache.lat is None and cache.fonte == 'google_fail':
        return None
    # Caso contrario (sem cache, ou cache de fonte antiga) — bate no Google

    coords = _geocode_remoto(endereco)
    if not cache:
        cache = GeocodeCache(chave=chave, fonte='google')
        db.session.add(cache)
    if coords:
        cache.lat, cache.lng = coords
        cache.fonte = 'google'
    else:
        cache.lat, cache.lng = None, None
        cache.fonte = 'google_fail'
    db.session.commit()
    return coords


def geocode_em_lote(enderecos, max_workers=8):
    """Geocoda lista de enderecos em paralelo. Retorna dict {endereco: (lat, lng) | None}.
    Usa cache. Persiste no banco em batch (commits parciais)."""
    if not enderecos:
        return {}

    # Pre-popula com hits do cache (sem fazer request)
    # So aceita cache hit confiavel (fonte=google). Cache de fonte antiga
    # (Nominatim/BrasilAPI/AwesomeAPI) eh re-tentado.
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
        elif cache and cache.lat is None and cache.fonte == 'google_fail':
            resultados[end] = None
        else:
            pendentes.append(end)

    if not pendentes:
        return resultados

    # Pra os pendentes, paraleliza
    novos = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_geocode_remoto, e): e for e in pendentes}
        for f in as_completed(futures):
            e = futures[f]
            try:
                novos[e] = f.result()
            except Exception:
                novos[e] = None

    # Persiste em batch
    for e, coords in novos.items():
        chave = _normalizar_chave(e)
        if not chave:
            continue
        cache = GeocodeCache.query.filter_by(chave=chave).first()
        if not cache:
            cache = GeocodeCache(chave=chave, fonte='google')
            db.session.add(cache)
        if coords:
            cache.lat, cache.lng = coords
            cache.fonte = 'google'
        else:
            cache.lat, cache.lng = None, None
            cache.fonte = 'google_fail'
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

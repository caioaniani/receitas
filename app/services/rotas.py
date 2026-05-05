"""Geracao de rotas de entrega: geocoding, clustering e ordenacao.

Estrategia:
1. Geocoda enderecos: AwesomeAPI (gratuita, rapida, paralelizavel) com fallback Nominatim.
2. Cache permanente em GeocodeCache pra evitar re-bater APIs.
3. Agrupa pontos em N rotas usando k-means simples (sem deps externas).
4. Em cada rota, ordena por nearest neighbor saindo da loja matriz.
"""

import logging
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from app.extensions import db
from app.models import GeocodeCache

logger = logging.getLogger(__name__)


# Loja matriz "1851" — coordenadas de origem das rotas. Configurável via env (RotaConfig).
# Default: centro de São Paulo. Ajustar no painel ou via variavel de ambiente.
_DEFAULT_ORIGEM_LAT = -23.5505
_DEFAULT_ORIGEM_LNG = -46.6333


def origem_padrao(app=None):
    """Retorna (lat, lng) da loja matriz. Le ROTA_ORIGEM_LAT/LNG do config."""
    if app is None:
        from flask import current_app
        app = current_app
    lat = app.config.get('ROTA_ORIGEM_LAT', _DEFAULT_ORIGEM_LAT)
    lng = app.config.get('ROTA_ORIGEM_LNG', _DEFAULT_ORIGEM_LNG)
    try:
        return float(lat), float(lng)
    except (TypeError, ValueError):
        return _DEFAULT_ORIGEM_LAT, _DEFAULT_ORIGEM_LNG


# ── Geocoding ──

def _normalizar_chave(endereco):
    """Chave de cache: minuscula, sem acentuacao excessiva, espacos colapsados."""
    if not endereco:
        return ''
    s = endereco.strip().lower()
    s = re.sub(r'\s+', ' ', s)
    return s[:200]


def _extrair_cep(endereco):
    """Tenta extrair CEP (8 digitos) do endereco."""
    if not endereco:
        return None
    m = re.search(r'(\d{5})-?(\d{3})', endereco)
    if m:
        return m.group(1) + m.group(2)
    m = re.search(r'\b(\d{8})\b', endereco)
    if m:
        return m.group(1)
    return None


_last_nominatim_call = [0.0]


def _consultar_brasilapi(cep):
    """Geocoding via BrasilAPI v2: gratuita, sem rate limit estrito.
    Retorna (lat, lng) ou None. So funciona com CEP brasileiro (8 digitos)."""
    if not cep or len(cep) != 8 or not cep.isdigit():
        return None
    try:
        r = requests.get(
            f'https://brasilapi.com.br/api/cep/v2/{cep}',
            headers={'User-Agent': 'OPaoPadariaERP/1.0'},
            timeout=5,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        loc = (data.get('location') or {}).get('coordinates') or {}
        lat = loc.get('latitude')
        lng = loc.get('longitude')
        if lat and lng:
            return float(lat), float(lng)
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None
    return None


def _consultar_awesomeapi(cep):
    """Fallback: AwesomeAPI tambem retorna lat/lng do CEP."""
    if not cep or len(cep) != 8 or not cep.isdigit():
        return None
    try:
        r = requests.get(f'https://cep.awesomeapi.com.br/json/{cep}', timeout=5)
        if r.status_code != 200:
            return None
        data = r.json()
        lat = data.get('lat')
        lng = data.get('lng')
        if lat and lng:
            return float(lat), float(lng)
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None
    return None


def _consultar_nominatim(query):
    """Bate em Nominatim respeitando rate limit de 1 req/s."""
    diff = time.time() - _last_nominatim_call[0]
    if diff < 1.1:
        time.sleep(1.1 - diff)
    _last_nominatim_call[0] = time.time()
    try:
        r = requests.get(
            'https://nominatim.openstreetmap.org/search',
            params={'q': query, 'format': 'json', 'limit': 1, 'countrycodes': 'br'},
            headers={'User-Agent': 'OPaoPadariaERP/1.0'},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not data:
            return None
        return float(data[0]['lat']), float(data[0]['lon'])
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        logger.warning('Nominatim falhou para %r: %s', query, e)
        return None


def _geocode_sem_cache(endereco):
    """Geocoda um endereco SEM tocar o banco. Pra uso em threads paralelas.
    Retorna (lat, lng, fonte). Tenta APIs de CEP rapidas antes de Nominatim."""
    cep = _extrair_cep(endereco)
    # 1. BrasilAPI (CEP — gratuita, rapida, paralelizavel)
    if cep:
        coords = _consultar_brasilapi(cep)
        if coords:
            return coords[0], coords[1], 'brasilapi'
        # 2. AwesomeAPI (CEP — fallback rapido)
        coords = _consultar_awesomeapi(cep)
        if coords:
            return coords[0], coords[1], 'awesomeapi'
    # 3. Nominatim — fallback (rate-limited, serial)
    coords = _consultar_nominatim(endereco)
    if coords:
        return coords[0], coords[1], 'nominatim'
    if cep:
        coords = _consultar_nominatim(f'{cep[:5]}-{cep[5:]} Brasil')
        if coords:
            return coords[0], coords[1], 'nominatim_cep'
    return None, None, 'falhou'


def geocode(endereco):
    """Retorna (lat, lng) do endereco. Usa cache se existir.
    Para uso pontual; para batch grande use _geocode_paralelo."""
    if not endereco:
        return None
    chave = _normalizar_chave(endereco)
    if not chave:
        return None

    cache = GeocodeCache.query.filter_by(chave=chave).first()
    if cache and cache.lat is not None:
        return cache.lat, cache.lng

    lat, lng, fonte = _geocode_sem_cache(endereco)

    if not cache:
        cache = GeocodeCache(chave=chave, fonte=fonte)
        db.session.add(cache)
    cache.lat = lat
    cache.lng = lng
    cache.fonte = fonte
    db.session.commit()
    return (lat, lng) if lat is not None else None


# ── Clustering (k-means simples) ──

def _haversine(p1, p2):
    """Distancia em km entre dois pontos (lat, lng)."""
    lat1, lng1 = p1
    lat2, lng2 = p2
    R = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def _kmeans(pontos, k, max_iter=20):
    """k-means simples 2D. pontos: lista de (lat, lng).
    Retorna lista de N (mesmo tamanho de pontos) com cluster_id 0..k-1.
    """
    if not pontos or k <= 0:
        return []
    if k == 1:
        return [0] * len(pontos)
    if len(pontos) <= k:
        return list(range(len(pontos)))

    # Init: pega k pontos espacados (deterministic) — pega o ponto mais distante
    # do centroide ja escolhido (k-means++ simplificado).
    centros = [pontos[0]]
    while len(centros) < k:
        # Pra cada ponto, distancia ao centro mais proximo
        max_d = -1
        max_p = None
        for p in pontos:
            d = min(_haversine(p, c) for c in centros)
            if d > max_d:
                max_d = d
                max_p = p
        centros.append(max_p)

    atribuicoes = [0] * len(pontos)
    for _ in range(max_iter):
        mudou = False
        # Atribuir cada ponto ao centro mais proximo
        for i, p in enumerate(pontos):
            melhor = 0
            melhor_d = _haversine(p, centros[0])
            for j in range(1, k):
                d = _haversine(p, centros[j])
                if d < melhor_d:
                    melhor_d = d
                    melhor = j
            if atribuicoes[i] != melhor:
                atribuicoes[i] = melhor
                mudou = True
        if not mudou:
            break
        # Recalcular centros (media de cada cluster)
        for j in range(k):
            membros = [pontos[i] for i in range(len(pontos)) if atribuicoes[i] == j]
            if membros:
                centros[j] = (
                    sum(p[0] for p in membros) / len(membros),
                    sum(p[1] for p in membros) / len(membros),
                )

    return atribuicoes


# ── Ordenacao (nearest neighbor) ──

def _ordenar_nearest_neighbor(pontos, origem):
    """Retorna lista de indices na ordem de visita, comecando do mais proximo da origem."""
    if not pontos:
        return []
    visitados = [False] * len(pontos)
    ordem = []
    atual = origem
    while not all(visitados):
        melhor_i = -1
        melhor_d = float('inf')
        for i, p in enumerate(pontos):
            if visitados[i]:
                continue
            d = _haversine(atual, p)
            if d < melhor_d:
                melhor_d = d
                melhor_i = i
        if melhor_i == -1:
            break
        ordem.append(melhor_i)
        visitados[melhor_i] = True
        atual = pontos[melhor_i]
    return ordem


# ── Geracao de rotas ──

def _geocodar_em_lote(pendentes_enderecos, max_seconds=40, max_workers=8):
    """Geocoda em paralelo via AwesomeAPI (CEP). ThreadPool.
    Retorna dict {endereco: (lat, lng, fonte)}. Enderecos sem resultado vem com (None, None, fonte).
    """
    resultados = {}
    if not pendentes_enderecos:
        return resultados

    inicio = time.time()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_geocode_sem_cache, end): end for end in pendentes_enderecos}
        try:
            tempo_restante = max(1, max_seconds - (time.time() - inicio))
            for future in as_completed(futures, timeout=tempo_restante):
                end = futures[future]
                try:
                    lat, lng, fonte = future.result(timeout=10)
                    resultados[end] = (lat, lng, fonte)
                except Exception:
                    resultados[end] = (None, None, 'erro')
        except Exception:  # TimeoutError ou outro
            pass
    return resultados


def gerar_rotas(pedidos, n_drivers, origem, max_seconds=40):
    """pedidos: lista de dicts com 'code', 'endereco', 'destinatario', etc.
    max_seconds: tempo maximo de geocoding (default 40s, abaixo do timeout do proxy).

    Retorna {'rotas': [...], 'sem_geocode': [...], 'tempo_geo': float, 'incomplete': bool}.
    """
    if n_drivers <= 0:
        n_drivers = 1

    inicio = time.time()
    com_coords = []
    sem_coords = []

    # Etapa 1: separa cache hits dos pendentes
    pendentes = []  # lista de (pedido, endereco, chave)
    for p in pedidos:
        end = p.get('endereco') or ''
        if not end:
            sem_coords.append(p)
            continue
        chave = _normalizar_chave(end)
        if not chave:
            sem_coords.append(p)
            continue
        cache = GeocodeCache.query.filter_by(chave=chave).first()
        if cache and cache.lat is not None:
            com_coords.append(dict(p, lat=cache.lat, lng=cache.lng))
            continue
        if cache and cache.lat is None:
            # Ja tentamos antes e nao achamos. Nao re-bate.
            sem_coords.append(p)
            continue
        pendentes.append((p, end, chave))

    # Etapa 2: geocoda pendentes em paralelo (AwesomeAPI)
    incomplete = False
    if pendentes:
        enderecos_unicos = list({e for _, e, _ in pendentes})
        resultados = _geocodar_em_lote(enderecos_unicos, max_seconds=max_seconds, max_workers=8)

        # Persiste no cache em batch
        for end in enderecos_unicos:
            chave = _normalizar_chave(end)
            if not chave:
                continue
            cache = GeocodeCache.query.filter_by(chave=chave).first()
            r = resultados.get(end)
            if r is None:
                # Nao processado nesse lote — timeout
                incomplete = True
                continue
            lat, lng, fonte = r
            if not cache:
                cache = GeocodeCache(chave=chave, fonte=fonte)
                db.session.add(cache)
            cache.lat = lat
            cache.lng = lng
            cache.fonte = fonte
        db.session.commit()

        # Distribui pelos pedidos pendentes
        for pedido, end, chave in pendentes:
            r = resultados.get(end)
            if r and r[0] is not None:
                com_coords.append(dict(pedido, lat=r[0], lng=r[1]))
            else:
                sem_coords.append(pedido)

    tempo_geo = time.time() - inicio

    if not com_coords:
        return {
            'rotas': [],
            'sem_geocode': sem_coords,
            'tempo_geo': tempo_geo,
            'incomplete': incomplete,
            'origem': {'lat': origem[0], 'lng': origem[1]},
        }

    n_drivers = min(n_drivers, len(com_coords))

    pts = [(p['lat'], p['lng']) for p in com_coords]
    clusters = _kmeans(pts, n_drivers)

    rotas = []
    for d in range(n_drivers):
        membros = [com_coords[i] for i in range(len(com_coords)) if clusters[i] == d]
        if not membros:
            continue
        membros_pts = [(m['lat'], m['lng']) for m in membros]
        ordem = _ordenar_nearest_neighbor(membros_pts, origem)
        paradas = []
        for pos, idx in enumerate(ordem):
            par = dict(membros[idx], ordem=pos + 1)
            paradas.append(par)
        # Distancia total: origem -> p1 -> p2 -> ... -> origem
        dist = 0.0
        atual = origem
        for par in paradas:
            dist += _haversine(atual, (par['lat'], par['lng']))
            atual = (par['lat'], par['lng'])
        dist += _haversine(atual, origem)  # retorno
        rotas.append({
            'driver': d + 1,
            'paradas': paradas,
            'distancia_km': round(dist, 1),
            'qtd_paradas': len(paradas),
        })

    return {
        'rotas': rotas,
        'sem_geocode': sem_coords,
        'tempo_geo': round(tempo_geo, 1),
        'incomplete': incomplete,
        'origem': {'lat': origem[0], 'lng': origem[1]},
    }

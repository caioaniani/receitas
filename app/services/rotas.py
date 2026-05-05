"""Geracao de rotas de entrega: geocoding, clustering e ordenacao.

Estrategia:
1. Geocoda enderecos via Nominatim (OpenStreetMap), com cache em GeocodeCache.
2. Agrupa pontos em N rotas usando k-means simples (sem deps externas).
3. Em cada rota, ordena por nearest neighbor saindo da loja matriz.
"""

import logging
import math
import re
import time

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


def geocode(endereco):
    """Retorna (lat, lng) do endereco. Usa cache se existir."""
    if not endereco:
        return None
    chave = _normalizar_chave(endereco)
    if not chave:
        return None

    cache = GeocodeCache.query.filter_by(chave=chave).first()
    if cache and cache.lat is not None:
        return cache.lat, cache.lng

    # Tenta primeiro o endereco completo, depois fallback so CEP+Brasil
    cep = _extrair_cep(endereco)
    tentativas = [endereco]
    if cep:
        tentativas.append(f'{cep[:5]}-{cep[5:]} Brasil')

    coords = None
    for q in tentativas:
        coords = _consultar_nominatim(q)
        if coords:
            break

    if not cache:
        cache = GeocodeCache(chave=chave, fonte='nominatim')
        db.session.add(cache)

    if coords:
        cache.lat, cache.lng = coords
    else:
        # Marca como tentado (lat=None) pra nao re-bater toda vez
        cache.lat, cache.lng = None, None

    db.session.commit()
    return coords


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

def gerar_rotas(pedidos, n_drivers, origem):
    """pedidos: lista de dicts com 'code', 'endereco', 'destinatario', etc.
    Retorna {'rotas': [{'driver': N, 'paradas': [...]}], 'sem_geocode': [...], 'tempo_geo': float}.
    """
    if n_drivers <= 0:
        n_drivers = 1

    inicio = time.time()
    com_coords = []
    sem_coords = []

    for p in pedidos:
        end = p.get('endereco') or ''
        coords = geocode(end) if end else None
        if coords:
            p_copy = dict(p, lat=coords[0], lng=coords[1])
            com_coords.append(p_copy)
        else:
            sem_coords.append(p)

    tempo_geo = time.time() - inicio

    if not com_coords:
        return {
            'rotas': [],
            'sem_geocode': sem_coords,
            'tempo_geo': tempo_geo,
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
        # Calcula distancia total da rota (origem -> p1 -> p2 -> ... -> origem)
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
        'origem': {'lat': origem[0], 'lng': origem[1]},
    }

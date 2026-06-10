"""Faixas de frete por distância até a padaria (Brooklin).

Fonte: mapa "Fretes O pão" do My Maps, exportado em KML pelo dono em
10/06/2026 ("Faixas de frete O Pão sem sobreposição"). São anéis
concêntricos de 1 km a partir da loja do Brooklin (Rua Ribeiro do Vale,
455): grátis até 1 km, e a cada km adicional soma R$5, até o limite de
15 km (R$70). Além de 15 km = fora da área de entrega do site.

O valor daqui é ESTIMATIVA pro atendimento (bot/equipe) — o valor que
vale é o do checkout do site. Se o dono redesenhar o mapa, atualizar as
constantes abaixo (e o teste de faixas).

Geocodificação (sem chave de API):
  1. CEP -> BrasilAPI v2 (devolve lat/lng pra maioria dos CEPs urbanos);
  2. fallback/endereço livre -> Nominatim (OpenStreetMap).
"""
import logging
import re
from math import asin, ceil, cos, radians, sin, sqrt

import requests

logger = logging.getLogger(__name__)

# Centro dos anéis (centroide do KML) = padaria do Brooklin.
CENTRO_LAT = -23.598678
CENTRO_LNG = -46.693661
KM_GRATIS = 1.0          # até aqui, frete grátis
VALOR_POR_KM = 5.0       # cada km adicional (anel de 1 km) soma R$5
RAIO_MAX_KM = 15.0       # além disso, fora da área de entrega do site

_TIMEOUT = 8
# Nominatim exige User-Agent identificável (politica de uso do OSM).
_UA = {'User-Agent': 'opao-padaria-atendimento/1.0 (gestao.opaopadariaartesanal.com.br)'}


def distancia_km(lat, lng):
    """Haversine até o centro dos anéis, em km."""
    dlat = radians(lat - CENTRO_LAT)
    dlng = radians(lng - CENTRO_LNG)
    a = (sin(dlat / 2) ** 2
         + cos(radians(CENTRO_LAT)) * cos(radians(lat)) * sin(dlng / 2) ** 2)
    return 2 * 6371.0 * asin(sqrt(a))


def valor_para_distancia(km):
    """Valor do frete pro anel onde a distância cai. None = fora da área.

    Limites batem com o KML: cada anel fecha no km cheio (faixa R$5 vai de
    1 a 2 km — 2.0 km ainda é R$5)."""
    if km is None or km < 0 or km > RAIO_MAX_KM:
        return None
    if km <= KM_GRATIS:
        return 0.0
    return VALOR_POR_KM * (ceil(km) - 1)


def _extrair_cep(texto):
    m = re.search(r'(\d{5})[\s.-]?(\d{3})', texto or '')
    return f'{m.group(1)}{m.group(2)}' if m else None


def _geocodificar_cep(cep):
    """BrasilAPI v2: CEP -> (lat, lng, rótulo) ou None (sem coords/erro)."""
    try:
        r = requests.get(f'https://brasilapi.com.br/api/cep/v2/{cep}',
                         timeout=_TIMEOUT)
        if r.status_code != 200:
            return None
        d = r.json()
        coords = ((d.get('location') or {}).get('coordinates') or {})
        lat, lng = coords.get('latitude'), coords.get('longitude')
        rotulo = ', '.join(x for x in (d.get('street'), d.get('neighborhood'),
                                       d.get('city')) if x)
        if lat and lng:
            return float(lat), float(lng), rotulo or f'CEP {cep}'
        # Sem coordenadas: devolve só o rótulo pro fallback geocodificar.
        return (None, None, rotulo) if rotulo else None
    except (requests.RequestException, ValueError):
        logger.warning('BrasilAPI falhou pro CEP %s', cep)
        return None


def _geocodificar_texto(texto):
    """Nominatim (OSM): endereço livre -> (lat, lng, rótulo) ou None."""
    consulta = texto.strip()
    if 'são paulo' not in consulta.lower() and 'sao paulo' not in consulta.lower():
        consulta += ', São Paulo, Brasil'
    try:
        r = requests.get('https://nominatim.openstreetmap.org/search',
                         params={'q': consulta, 'format': 'json', 'limit': 1,
                                 'countrycodes': 'br'},
                         headers=_UA, timeout=_TIMEOUT)
        if r.status_code != 200:
            return None
        hits = r.json()
        if not hits:
            return None
        h = hits[0]
        return float(h['lat']), float(h['lon']), h.get('display_name', consulta)
    except (requests.RequestException, ValueError, KeyError):
        logger.warning('Nominatim falhou pra %r', texto)
        return None


def geocodificar(endereco_ou_cep):
    """(lat, lng, rotulo) pra um CEP ou endereço livre, ou None.

    CEP via BrasilAPI (com fallback do endereço resolvido no Nominatim);
    texto livre direto no Nominatim. Usado pelo frete do bot e pela
    integração Lalamove (origem/destino das corridas)."""
    texto = (endereco_ou_cep or '').strip()
    if not texto:
        return None
    geo = None
    cep = _extrair_cep(texto)
    if cep:
        geo = _geocodificar_cep(cep)
        if geo and geo[0] is None:
            # BrasilAPI conhece o CEP mas nao tem coordenada: geocodifica o
            # endereço resolvido (rua + bairro + cidade), mais preciso que o
            # texto cru do cliente.
            geo = _geocodificar_texto(geo[2])
    if not geo or geo[0] is None:
        geo = _geocodificar_texto(texto)
    if not geo or geo[0] is None:
        return None
    return geo


def consultar_frete(endereco_ou_cep):
    """Estimativa de frete pra um CEP ou endereço.

    Retorna:
      {'ok': True, 'valor': 15.0, 'gratis': False, 'fora_area': False,
       'distancia_km': 3.4, 'endereco': 'Rua X, Moema, São Paulo',
       'aviso': 'valor estimado — o definitivo é o do checkout'}
      {'ok': True, 'fora_area': True, ...}  -> além de 15 km
      {'ok': False, 'erro': 'endereco_vazio'|'nao_encontrado'}
    """
    if not (endereco_ou_cep or '').strip():
        return {'ok': False, 'erro': 'endereco_vazio'}
    geo = geocodificar(endereco_ou_cep)
    if not geo:
        return {'ok': False, 'erro': 'nao_encontrado'}

    lat, lng, rotulo = geo
    km = distancia_km(lat, lng)
    valor = valor_para_distancia(km)
    if valor is None:
        return {'ok': True, 'fora_area': True, 'distancia_km': round(km, 1),
                'endereco': rotulo,
                'aviso': 'fora do raio de 15 km — confirmar com a equipe'}
    return {'ok': True, 'fora_area': False, 'valor': valor,
            'gratis': valor == 0.0, 'distancia_km': round(km, 1),
            'endereco': rotulo,
            'aviso': 'valor estimado — o definitivo é o do checkout do site'}

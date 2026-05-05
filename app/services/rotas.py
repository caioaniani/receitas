"""Geracao de rotas de entrega — agrupamento simples por CEP.

Estrategia:
1. Pega pedidos do dia.
2. Ordena por CEP (proximidade geografica em SP).
3. Divide em N partes iguais entre os drivers.
4. UI gera links do Google Maps com waypoints — Google faz o geocoding
   e a otimizacao da ordem (ate 10 paradas por link).

Esse modulo deliberadamente NAO faz geocoding nosso. Aprendemos que APIs
gratuitas (Nominatim, BrasilAPI, AwesomeAPI) sao instaveis em producao,
e Google Maps Platform e cara/burocratica. Delegamos pro app do Maps.
"""

import logging
import math
import re

logger = logging.getLogger(__name__)


def _extrair_cep(endereco):
    """Tenta extrair CEP (8 digitos) do endereco. Retorna string ou ''."""
    if not endereco:
        return ''
    m = re.search(r'(\d{5})-?(\d{3})', endereco)
    if m:
        return m.group(1) + m.group(2)
    m = re.search(r'\b(\d{8})\b', endereco)
    if m:
        return m.group(1)
    return ''


def origem_endereco(app=None):
    """Endereco textual da matriz (pra link do Maps). Vazio = nao configurado."""
    if app is None:
        from flask import current_app
        app = current_app
    return (app.config.get('ROTA_ORIGEM_ENDERECO') or '').strip()


def gerar_rotas(pedidos, n_drivers):
    """Agrupa pedidos por CEP e divide entre N drivers.
    Sem geocoding: o link do Google Maps faz isso.

    Retorna {'rotas': [{'driver': N, 'paradas': [...], 'qtd_paradas': N}], 'sem_cep': [...]}.
    """
    n = max(1, n_drivers)

    com_cep = []
    sem_cep = []
    for p in pedidos:
        cep = _extrair_cep(p.get('endereco') or '')
        if cep:
            com_cep.append((cep, p))
        else:
            sem_cep.append(p)

    # Ordena por CEP (em SP, CEPs proximos = bairros proximos)
    com_cep.sort(key=lambda x: x[0])
    pedidos_ordenados = [p for _, p in com_cep]

    rotas = []
    total = len(pedidos_ordenados)
    if total == 0:
        return {'rotas': [], 'sem_cep': sem_cep}

    n = min(n, total)
    chunk_size = math.ceil(total / n)
    for d in range(n):
        start = d * chunk_size
        end = min(start + chunk_size, total)
        paradas_chunk = pedidos_ordenados[start:end]
        if not paradas_chunk:
            continue
        paradas = [dict(p, ordem=i + 1) for i, p in enumerate(paradas_chunk)]
        rotas.append({
            'driver': d + 1,
            'paradas': paradas,
            'qtd_paradas': len(paradas),
        })

    return {'rotas': rotas, 'sem_cep': sem_cep}

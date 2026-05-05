"""Geracao de rotas de entrega — agrupamento simples por CEP + drivers nominais.

Estrategia:
1. Pega pedidos do dia.
2. Pedidos com atribuicao salva (Driver X) ja vao pra coluna do driver, na ordem salva.
3. Pedidos sem atribuicao sao agrupados por CEP e distribuidos entre drivers ativos
   (chunks consecutivos pra manter proximidade).
4. UI gera links do Google Maps com waypoints.

Esse modulo deliberadamente NAO faz geocoding. Aprendemos que APIs gratuitas
sao instaveis e Google Maps Platform e cara.
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


def gerar_rotas(pedidos, drivers, atribuicoes=None):
    """Distribui pedidos entre drivers nominais.

    drivers: lista de dicts {id, nome, cor}.
    atribuicoes: dict {pedido_code: {'driver_id': int|None, 'ordem': int}}.

    Pedidos com atribuicao salva ficam com seu driver original, na ordem salva.
    Pedidos sem atribuicao sao distribuidos entre drivers ativos por proximidade
    de CEP (chunks consecutivos).

    Retorna {'rotas': [{'driver': {...}, 'paradas': [...], 'qtd_paradas': N}], 'sem_cep': [...]}.
    """
    atribuicoes = atribuicoes or {}
    if not drivers:
        return {'rotas': [], 'sem_cep': []}

    drivers_por_id = {d['id']: d for d in drivers}

    # 1. Separa pedidos pre-atribuidos vs novos
    pre_atribuidos = {}  # driver_id -> [(ordem, pedido)]
    nao_atribuidos = []
    sem_cep = []

    for p in pedidos:
        code = p.get('code')
        atrib = atribuicoes.get(code)
        if atrib and atrib.get('driver_id') in drivers_por_id:
            did = atrib['driver_id']
            ordem = atrib.get('ordem', 0)
            pre_atribuidos.setdefault(did, []).append((ordem, p))
        else:
            nao_atribuidos.append(p)

    # 2. Pra os nao atribuidos: agrupa por CEP + distribui em chunks consecutivos
    com_cep = []
    for p in nao_atribuidos:
        cep = _extrair_cep(p.get('endereco') or '')
        if cep:
            com_cep.append((cep, p))
        else:
            sem_cep.append(p)
    com_cep.sort(key=lambda x: x[0])

    pedidos_para_distribuir = [p for _, p in com_cep]
    n = len(drivers)
    total = len(pedidos_para_distribuir)
    chunk_size = math.ceil(total / n) if total else 0

    distribuidos = {d['id']: [] for d in drivers}
    for i in range(n):
        if not chunk_size:
            break
        start = i * chunk_size
        end = min(start + chunk_size, total)
        if start >= total:
            break
        chunk = pedidos_para_distribuir[start:end]
        distribuidos[drivers[i]['id']] = chunk

    # 3. Monta rotas finais: pre-atribuidos primeiro (ordem salva) + distribuidos
    rotas = []
    for d in drivers:
        did = d['id']
        # Pre-atribuidos: ordena por ordem salva
        atrib_list = sorted(pre_atribuidos.get(did, []), key=lambda x: x[0])
        paradas_atrib = [p for _, p in atrib_list]
        # Distribuidos: ja ordenado por CEP
        paradas_novos = distribuidos.get(did, [])

        todas = paradas_atrib + paradas_novos
        if not todas:
            continue
        paradas = [dict(p, ordem=idx + 1) for idx, p in enumerate(todas)]
        rotas.append({
            'driver': {'id': d['id'], 'nome': d['nome'], 'cor': d.get('cor')},
            'paradas': paradas,
            'qtd_paradas': len(paradas),
        })

    return {'rotas': rotas, 'sem_cep': sem_cep}

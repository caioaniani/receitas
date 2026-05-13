"""Geracao de rotas de entrega.

Estrategia (com GOOGLE_MAPS_API_KEY):
1. Geocoda enderecos via Google Geocoding API (cache permanente).
2. K-means real sobre lat/lng → clusters geograficos.
3. Pra cada cluster: Google Directions API com optimize_waypoints reordena
   na rota mais curta + retorna km e tempo.

Fallback (sem chave): agrupa por CEP e divide em chunks. Sem otimizacao
real e sem distancia/tempo.
"""

import logging
import math
import re

from app.services import google_maps

logger = logging.getLogger(__name__)


def _extrair_cep(endereco):
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
    if app is None:
        from flask import current_app
        app = current_app
    return (app.config.get('ROTA_ORIGEM_ENDERECO') or '').strip()


def origem_latlng(app=None):
    """Lat/lng da matriz: ou via vars de env, ou geocodando o endereco."""
    if app is None:
        from flask import current_app
        app = current_app
    lat = app.config.get('ROTA_ORIGEM_LAT')
    lng = app.config.get('ROTA_ORIGEM_LNG')
    try:
        if lat and lng:
            return float(lat), float(lng)
    except (TypeError, ValueError):
        pass
    end = origem_endereco(app)
    if end:
        coords = google_maps.geocode(end)
        if coords:
            return coords
    return None


def _haversine(p1, p2):
    """Distancia km entre dois pontos."""
    R = 6371.0
    lat1, lng1 = p1
    lat2, lng2 = p2
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def _kmeans(pontos, k, max_iter=20):
    """k-means simples 2D. Retorna lista de cluster_id pra cada ponto."""
    if not pontos or k <= 0:
        return []
    if k == 1:
        return [0] * len(pontos)
    if len(pontos) <= k:
        return list(range(len(pontos)))

    # Init k-means++ simplificado: pega o ponto mais distante a cada passo
    centros = [pontos[0]]
    while len(centros) < k:
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
        for j in range(k):
            membros = [pontos[i] for i in range(len(pontos)) if atribuicoes[i] == j]
            if membros:
                centros[j] = (
                    sum(p[0] for p in membros) / len(membros),
                    sum(p[1] for p in membros) / len(membros),
                )
    return atribuicoes


def _refinar_clusters(pontos, atribuicoes, n_drivers, max_raio_km=8.0, max_iter=50):
    """Pos-processo de k-means que prioriza COMPACIDADE com balanceamento leve.

    Move iterativamente o ponto mais distante do seu centroide pra outro cluster,
    se isso reduzir o raio. Roda 2 fases:

    1. Outliers (raio > max_raio_km): move pra cluster mais proximo.
    2. Balanceamento: se algum cluster tem 3x+ pedidos que outro, move ponto
       de borda do mais cheio pra menos cheio (se o destino estiver razoavelmente
       proximo).
    """
    if not pontos or len(pontos) < 2 or n_drivers < 2:
        return atribuicoes

    # Fase 1: reduzir outliers
    PESO_BAL_FASE1 = 0.05  # quase zero — quer compacidade, nao balancear carga
    for _ in range(max_iter):
        # Centroides atuais
        centros = []
        for d in range(n_drivers):
            membros = [pontos[i] for i in range(len(pontos)) if atribuicoes[i] == d]
            if membros:
                centros.append((
                    sum(p[0] for p in membros) / len(membros),
                    sum(p[1] for p in membros) / len(membros),
                ))
            else:
                centros.append(None)

        # Acha o ponto MAIS distante do seu centroide
        pior_dist = 0.0
        pior_idx = -1
        for i, p in enumerate(pontos):
            cluster = atribuicoes[i]
            if cluster < 0 or cluster >= n_drivers or centros[cluster] is None:
                continue
            d = _haversine(p, centros[cluster])
            if d > pior_dist:
                pior_dist = d
                pior_idx = i

        # Se ja esta dentro do raio aceitavel, terminou
        if pior_dist <= max_raio_km or pior_idx < 0:
            break

        cluster_atual = atribuicoes[pior_idx]
        contagens = [sum(1 for a in atribuicoes if a == d) for d in range(n_drivers)]

        # Procura o melhor destino: minimiza dist + peso*contagem
        melhor_alvo = -1
        melhor_score = float('inf')
        for d in range(n_drivers):
            if d == cluster_atual or centros[d] is None:
                continue
            dist = _haversine(pontos[pior_idx], centros[d])
            score = dist + contagens[d] * PESO_BAL_FASE1
            if score < melhor_score:
                melhor_score = score
                melhor_alvo = d

        if melhor_alvo == -1:
            break

        # Verifica se a mudanca melhora: dist no destino < dist atual
        if _haversine(pontos[pior_idx], centros[melhor_alvo]) >= pior_dist - 0.5:
            break

        atribuicoes[pior_idx] = melhor_alvo

    # Fase 2: balanceamento — equaliza ate diferenca de no maximo 2 entre clusters,
    # movendo pontos de borda do mais cheio pro menos cheio quando geograficamente faz sentido.
    raio_aceitavel = max_raio_km * 1.6  # mais permissivo que Fase 1 (que cuida de outliers)
    for _ in range(max_iter):
        contagens = [sum(1 for a in atribuicoes if a == d) for d in range(n_drivers)]
        contagens_validas = [c for c in contagens if c > 0]
        if not contagens_validas:
            break
        max_c = max(contagens_validas)
        min_c = min(contagens_validas)
        if max_c - min_c <= 2:
            break

        # Recalcula centroides
        centros = []
        for d in range(n_drivers):
            membros = [pontos[i] for i in range(len(pontos)) if atribuicoes[i] == d]
            if membros:
                centros.append((
                    sum(p[0] for p in membros) / len(membros),
                    sum(p[1] for p in membros) / len(membros),
                ))
            else:
                centros.append(None)

        cluster_cheio = contagens.index(max_c)
        cluster_vazio = -1
        for d in range(n_drivers):
            if contagens[d] == min_c and centros[d] is not None:
                cluster_vazio = d
                break
        if cluster_vazio == -1:
            break

        # Pega o ponto do cluster_cheio que mais "ganha" indo pro cluster_vazio:
        # ou seja, aquele cuja distancia ao novo centroide e menor (ou nao muito maior)
        # que ao centroide atual. Privilegia pontos de borda.
        melhor_idx = -1
        melhor_ganho = float('inf')  # menor (mais negativo) = melhor
        for i in range(len(pontos)):
            if atribuicoes[i] != cluster_cheio:
                continue
            d_dest = _haversine(pontos[i], centros[cluster_vazio])
            if d_dest > raio_aceitavel:
                continue
            d_orig = _haversine(pontos[i], centros[cluster_cheio]) if centros[cluster_cheio] else float('inf')
            # ganho negativo = ja era mais proximo do destino. ganho pequeno positivo = ok mover.
            ganho = d_dest - d_orig
            if ganho < melhor_ganho:
                melhor_ganho = ganho
                melhor_idx = i

        if melhor_idx == -1:
            break  # Nao ha ponto razoavelmente proximo do cluster_vazio
        # so move se realmente faz sentido — destino nao pode ser muito pior que origem
        if melhor_ganho > 2.0:
            break
        atribuicoes[melhor_idx] = cluster_vazio

    return atribuicoes


def gerar_rotas(pedidos, drivers, atribuicoes=None, app=None):
    """Distribui pedidos entre drivers nominais. Usa Google quando disponivel.

    drivers: lista de {id, nome, cor}.
    atribuicoes: dict {pedido_code: {'driver_id': int|None, 'ordem': int}}.

    Pedidos com atribuicao salva ficam com seu driver original (em ordem salva).
    Pedidos sem atribuicao sao distribuidos por proximidade (lat/lng com Google,
    ou por CEP se Google nao disponivel).
    """
    atribuicoes = atribuicoes or {}
    if not drivers:
        return {'rotas': [], 'sem_cep': [], 'sem_atribuir': list(pedidos)}

    if app is None:
        from flask import current_app
        app = current_app

    drivers_por_id = {d['id']: d for d in drivers}
    tem_google = bool((app.config.get('GOOGLE_MAPS_API_KEY') or '').strip())

    # 1. Separa pre-atribuidos vs nao
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

    # 2. Geocoda os nao atribuidos (se Google), senao agrupa por CEP
    distribuidos = {d['id']: [] for d in drivers}

    if tem_google and nao_atribuidos:
        # Geocoda em lote
        enderecos = [p.get('endereco') or '' for p in nao_atribuidos]
        coords_map = google_maps.geocode_em_lote(enderecos)

        com_coords = []
        for p, end in zip(nao_atribuidos, enderecos):
            coords = coords_map.get(end)
            if coords:
                com_coords.append({**p, 'lat': coords[0], 'lng': coords[1]})
            else:
                # Sem geocode → cai pra agrupamento por CEP (pode entrar em algum cluster)
                sem_cep.append(p)

        # k-means real sobre lat/lng + refinamento (compacidade)
        if com_coords:
            n = min(len(drivers), len(com_coords))
            pts = [(p['lat'], p['lng']) for p in com_coords]
            clusters = _kmeans(pts, n)
            # Pos-processo: move outliers pra reduzir raio dos clusters
            clusters = _refinar_clusters(pts, clusters, n, max_raio_km=8.0)
            for i, p in enumerate(com_coords):
                cluster_id = clusters[i]
                if cluster_id < len(drivers):
                    distribuidos[drivers[cluster_id]['id']].append(p)
    elif nao_atribuidos:
        # Fallback: ordenar por CEP, dividir em chunks
        com_cep = []
        for p in nao_atribuidos:
            cep = _extrair_cep(p.get('endereco') or '')
            if cep:
                com_cep.append((cep, p))
            else:
                sem_cep.append(p)
        com_cep.sort(key=lambda x: x[0])
        ped_dist = [p for _, p in com_cep]
        n = min(len(drivers), len(ped_dist))
        if n > 0:
            chunk = math.ceil(len(ped_dist) / n)
            for i in range(n):
                inicio = i * chunk
                fim = min(inicio + chunk, len(ped_dist))
                if inicio >= len(ped_dist):
                    break
                distribuidos[drivers[i]['id']] = ped_dist[inicio:fim]

    # 2.5. Aplica capacidade de cada driver. Sobras viram excedentes que
    # tentam encaixar em outros drivers com vaga; o que sobrar fica em
    # 'sem_atribuir'.
    logger.info('rotas: %d pedidos, %d drivers, %d pre_atribuidos, %d nao_atribuidos, %d distribuidos via clustering',
                len(pedidos), len(drivers),
                sum(len(v) for v in pre_atribuidos.values()),
                len(nao_atribuidos),
                sum(len(v) for v in distribuidos.values()))
    for d in drivers:
        logger.info('rotas: driver %s cap=%s pre=%d kmeans_atribuiu=%d',
                    d.get('nome'), d.get('capacidade'),
                    len(pre_atribuidos.get(d['id'], [])),
                    len(distribuidos.get(d['id']) or []))
    excedentes = []
    for d in drivers:
        cap = d.get('capacidade') or 999
        ja_tem = len(pre_atribuidos.get(d['id'], []))
        vagas = max(0, cap - ja_tem)
        novos = distribuidos.get(d['id']) or []
        if len(novos) > vagas:
            distribuidos[d['id']] = novos[:vagas]
            excedentes.extend(novos[vagas:])

    # Tenta encaixar excedentes em drivers com vagas restantes (round-robin)
    if excedentes:
        i = 0
        sem_atribuir = []
        for p in excedentes:
            colocado = False
            tentativas = 0
            while tentativas < len(drivers):
                d = drivers[i % len(drivers)]
                cap = d.get('capacidade') or 999
                total_atual = len(pre_atribuidos.get(d['id'], [])) + len(distribuidos.get(d['id'], []))
                if total_atual < cap:
                    distribuidos[d['id']].append(p)
                    colocado = True
                    i += 1
                    break
                i += 1
                tentativas += 1
            if not colocado:
                sem_atribuir.append(p)
    else:
        sem_atribuir = []

    logger.info('rotas: final → %d distribuidos, %d sem_atribuir, %d sem_cep',
                sum(len(v) for v in distribuidos.values()),
                len(sem_atribuir),
                len(sem_cep))

    # 3. Otimiza ordem dentro de cada rota com Google Directions (se disponivel)
    origem = origem_latlng(app) if tem_google else None

    rotas = []
    for d in drivers:
        did = d['id']
        # Pre-atribuidos: respeita ordem salva
        atrib_list = sorted(pre_atribuidos.get(did, []), key=lambda x: x[0])
        paradas_atrib = [p for _, p in atrib_list]
        paradas_novos = distribuidos.get(did, [])
        todas = paradas_atrib + paradas_novos
        if not todas:
            continue

        km = None
        minutos = None

        # Garante lat/lng pra todas as paradas (geocoda as que faltam).
        # Crucial pro mapa visual — sem lat/lng, parada nao aparece no Leaflet.
        if tem_google:
            paradas_com_coords = []
            for p in todas:
                if 'lat' in p and 'lng' in p:
                    paradas_com_coords.append(p)
                else:
                    coords = google_maps.geocode(p.get('endereco') or '')
                    if coords:
                        paradas_com_coords.append({**p, 'lat': coords[0], 'lng': coords[1]})
                    else:
                        paradas_com_coords.append(p)
            todas = paradas_com_coords

        # Otimiza ordem com Directions API se: origem geocodada + todas com coords
        if tem_google and origem and all('lat' in p for p in todas):
            MAX_WAYPOINTS = 25
            latlngs = [(p['lat'], p['lng']) for p in todas]

            if len(latlngs) <= MAX_WAYPOINTS:
                resultado = google_maps.directions_otimizado(origem, latlngs, retorno_origem=True)
                if resultado:
                    todas = [todas[i] for i in resultado['ordem']]
                    km = resultado['km']
                    minutos = resultado['minutos']
            else:
                # >25 paradas: chunks de 25, otimiza cada, concatena km/min
                nova_ordem_global = []
                km_total = 0.0
                min_total = 0
                chunks_ok = True
                for i in range(0, len(latlngs), MAX_WAYPOINTS):
                    chunk = latlngs[i:i + MAX_WAYPOINTS]
                    r = google_maps.directions_otimizado(origem, chunk, retorno_origem=True)
                    if r:
                        nova_ordem_global.extend(i + idx for idx in r['ordem'])
                        km_total += r['km']
                        min_total += r['minutos']
                    else:
                        nova_ordem_global.extend(range(i, min(i + MAX_WAYPOINTS, len(latlngs))))
                        chunks_ok = False
                todas = [todas[idx] for idx in nova_ordem_global]
                if chunks_ok:
                    km = round(km_total, 1)
                    minutos = min_total

        paradas = [{**p, 'ordem': idx + 1} for idx, p in enumerate(todas)]
        rotas.append({
            'driver': {'id': d['id'], 'nome': d['nome'], 'cor': d.get('cor')},
            'paradas': paradas,
            'qtd_paradas': len(paradas),
            'km': km,
            'minutos': minutos,
        })

    return {'rotas': rotas, 'sem_cep': sem_cep, 'sem_atribuir': sem_atribuir}

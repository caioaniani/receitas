"""Distribuição de rotas — fixes do Dia dos Pais (09/08/2026, mapa
retalhado): (1) excedente de capacidade vai pro driver com vaga MAIS
PRÓXIMO (era round-robin cego); (2) `?reagrupar=1` devolve TODO pendente
pro pool e re-agrupa do zero (entregue/nao_entregue intocados) — sem isso
cada Auto-distribuir agrupava só as sobras e rodar 2x retalhava o mapa."""
from unittest.mock import patch

from app.extensions import db
from app.models import AtribuicaoEntrega, Driver, LoteSaida, PedidoOnline
from app.utils import hoje


def test_excedente_vai_pro_cluster_mais_proximo(app):
    from app.services import rotas as svc
    # 3 drivers: d1 lota (cap 1); d3 LONGE vem antes de d2 PERTO na lista —
    # o round-robin antigo mandaria o excedente pro d3 (primeiro com vaga).
    drivers = [
        {'id': 1, 'nome': 'Lotado', 'cor': '#111111', 'capacidade': 1},
        {'id': 3, 'nome': 'Longe', 'cor': '#333333', 'capacidade': 99},
        {'id': 2, 'nome': 'Perto', 'cor': '#222222', 'capacidade': 99},
    ]
    pedidos = [
        {'code': 'P0', 'endereco': 'A', 'destinatario': 'x'},
        {'code': 'P1', 'endereco': 'B', 'destinatario': 'x'},
        {'code': 'P2', 'endereco': 'C', 'destinatario': 'x'},
        {'code': 'P3', 'endereco': 'D', 'destinatario': 'x'},
    ]
    coords = {'A': (-23.60, -46.68), 'B': (-23.601, -46.681),
              'C': (-23.602, -46.679), 'D': (-23.50, -46.40)}
    app.config['GOOGLE_MAPS_API_KEY'] = 'fake'
    with app.app_context(), \
            patch.object(svc.google_maps, 'geocode_em_lote',
                         side_effect=lambda ends: {e: coords[e] for e in ends}), \
            patch.object(svc.google_maps, 'geocode',
                         side_effect=lambda e: coords.get(e)), \
            patch.object(svc.google_maps, 'directions_otimizado',
                         return_value=None), \
            patch.object(svc, '_kmeans',
                         return_value=[0, 0, 1, 2]), \
            patch.object(svc, '_refinar_clusters',
                         side_effect=lambda pts, cl, n, **kw: cl):
        # cluster0 = P0+P1 (vizinhos) -> Lotado cap 1 => P1 vira excedente;
        # cluster1 = P2 (do lado de P1) -> Longe? NAO: índice 1 = drivers[1]
        # = Longe... clusters por índice: 0->Lotado, 1->Longe, 2->Perto.
        # P2 cai no Longe e P3 no Perto — o excedente P1 (em -23.60) deve
        # ir pro driver de cluster mais PRÓXIMO dele: Longe tem P2 vizinho,
        # Perto tem P3 distante => vai pro Longe (que aqui está perto de
        # P1). O que NÃO pode é ignorar geografia.
        r = svc.gerar_rotas(pedidos, drivers, atribuicoes={}, app=app)
    por_driver = {rt['driver']['id']: [p['code'] for p in rt['paradas']]
                  for rt in r['rotas']}
    assert por_driver[1] == ['P0'] or por_driver[1] == ['P1']   # cap 1
    excedente = 'P1' if 'P0' in por_driver[1] else 'P0'
    assert excedente in por_driver[3]       # foi pro cluster VIZINHO (P2)
    assert excedente not in por_driver.get(2, [])


def _pedido_online(codigo):
    p = PedidoOnline(codigo=codigo, status='pago', nome_cliente='C',
                     email_cliente=f'{codigo}@x.com', modo_entrega='agendada',
                     data_entrega=hoje(), janela_entrega='06:00–10:00',
                     valor_total=100,
                     endereco_entrega='Rua X, 1, São Paulo, 04000-000')
    db.session.add(p)
    return p


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def test_reagrupar_devolve_pendente_pro_pool(app, admin_user):
    d1 = Driver(nome='D Um', ativo=True, token='tok-rg-1', capacidade=99)
    d2 = Driver(nome='D Dois', ativo=True, token='tok-rg-2', capacidade=99)
    lote = LoteSaida(nome='L1', data_entrega=hoje())
    db.session.add_all([d1, d2, lote])
    _pedido_online('RG1')
    _pedido_online('RG2')
    db.session.flush()
    # RG1: pendente, já atribuído ao d1 num lote anterior.
    # RG2: ENTREGUE pelo d1 — nunca pode voltar pro pool.
    db.session.add(AtribuicaoEntrega(pedido_code='RG1', driver_id=d1.id,
                                     lote_id=lote.id, data_entrega=hoje(),
                                     ordem=1, status='pendente'))
    db.session.add(AtribuicaoEntrega(pedido_code='RG2', driver_id=d1.id,
                                     lote_id=lote.id, data_entrega=hoje(),
                                     ordem=2, status='entregue'))
    db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    base = f'/entregas/api/rotas?data={hoje().isoformat()}'

    # SEM reagrupar: RG1 (atribuído em outro lote) fica FORA do pool.
    r1 = client.get(f'{base}&drivers={d2.id}')
    codes1 = [p['code'] for rt in r1.get_json()['rotas']
              for p in rt['paradas']]
    assert 'RG1' not in codes1 and 'RG2' not in codes1

    # COM reagrupar: RG1 volta pro pool e cai no driver selecionado;
    # RG2 (entregue) segue intocado, fora do pool.
    r2 = client.get(f'{base}&drivers={d2.id}&reagrupar=1')
    codes2 = [p['code'] for rt in r2.get_json()['rotas']
              for p in rt['paradas']]
    assert 'RG1' in codes2
    assert 'RG2' not in codes2


def test_leve_e_reotimizar_incluem_os_atribuidos(app, admin_user):
    """Madrugada do Dia dos Pais: com o dia TODO distribuido em lote, o
    /api/rotas vinha VAZIO (exclusao de 05/2026, feita pro fluxo de
    distribuir) — mapa sem pinos e "Nada a re-otimizar". `leve=1` (mapa) e
    `reotimizar=1` (botao) incluem os atribuidos com driver+ordem salvos."""
    d1 = Driver(nome='D Mapa', ativo=True, token='tok-mp-1', capacidade=99)
    lote = LoteSaida(nome='L2', data_entrega=hoje())
    db.session.add_all([d1, lote])
    _pedido_online('MP1')
    db.session.flush()
    db.session.add(AtribuicaoEntrega(pedido_code='MP1', driver_id=d1.id,
                                     lote_id=lote.id, data_entrega=hoje(),
                                     ordem=7, status='pendente'))
    db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    base = f'/entregas/api/rotas?data={hoje().isoformat()}'

    # Sem flag: excluido (contrato de 05/2026 preservado pro distribuir)
    r0 = client.get(base)
    assert all(p['code'] != 'MP1' for rt in r0.get_json()['rotas']
               for p in rt['paradas'])

    for flag in ('leve=1', 'reotimizar=1'):
        r = client.get(f'{base}&{flag}')
        achado = [(rt['driver']['id'], p['code'])
                  for rt in r.get_json()['rotas']
                  for p in rt['paradas'] if p['code'] == 'MP1']
        assert achado == [(d1.id, 'MP1')], flag   # com o driver salvo


def test_balancear_clusters_nivela_o_tamanho():
    """14 paradas pra um e 1 pro outro (mapa do Dia dos Pais): o cluster
    cheio repassa a parada mais proxima de quem tem folga; teto justo."""
    from app.services.rotas import _balancear_clusters
    # 6 pontos juntos no "centro" + 1 isolado longe; 2 clusters.
    pts = [(0.0, 0.0), (0.0, 0.01), (0.01, 0.0), (0.01, 0.01),
           (0.02, 0.0), (0.02, 0.01), (2.0, 2.0)]
    clusters = [0, 0, 0, 0, 0, 0, 1]        # 6 x 1
    out = _balancear_clusters(pts, list(clusters), 2)
    sizes = [out.count(0), out.count(1)]
    assert max(sizes) <= 4                  # ceil(7/2) = 4: nivelado
    assert out[6] == 1                      # o isolado fica onde esta


def test_polir_desfaz_o_grupo_lixao():
    """"O algoritmo ferra com o amarelo": pontas soltas de regioes opostas
    caiam no MESMO cluster (o coletor). O polimento troca pares ate cada
    ponto ficar com o grupo da SUA regiao — sem mudar os tamanhos."""
    from app.services.rotas import _polir_clusters
    # Blob A em ~(0,0), blob B em ~(1,1); um ponto de cada preso no grupo
    # errado (o padrao do lixao).
    pts = [(0.0, 0.0), (0.0, 0.01), (0.01, 0.0), (1.0, 1.01),
           (1.0, 1.0), (1.01, 1.0), (1.01, 1.01), (0.01, 0.01)]
    clusters = [0, 0, 0, 0, 1, 1, 1, 1]      # pts[3] e pts[7] trocados
    out = _polir_clusters(pts, list(clusters), 2)
    assert out[3] == 1 and out[7] == 0       # cada um voltou pra sua regiao
    assert sorted([out.count(0), out.count(1)]) == [4, 4]   # tamanhos iguais


def test_ancoragem_cluster_vai_pro_driver_da_zona(app):
    """"Roteirizar sobras embaralhou tudo": o vinculo cluster->driver era
    pelo INDICE (ordem alfabetica). Agora o bolsao novo vai pro motorista
    cujas paradas JA atribuidas estao mais perto — William com paradas do
    lado da padaria recebe as sobras DA PADARIA, nao as de Sao Bernardo."""
    from app.services import rotas as svc
    drivers = [
        {'id': 1, 'nome': 'Aaa Longe', 'cor': '#111111', 'capacidade': 99},
        {'id': 2, 'nome': 'Zzz William', 'cor': '#222222', 'capacidade': 99},
    ]
    # William (id 2) JA tem uma parada na "padaria" (-23.60); o driver Aaa
    # (id 1, PRIMEIRO da lista) ja esta em "Sao Bernardo" (-23.70).
    atribuicoes = {'W1': {'driver_id': 2, 'ordem': 1},
                   'L1': {'driver_id': 1, 'ordem': 1}}
    pedidos = [
        {'code': 'W1', 'endereco': 'PAD1', 'destinatario': 'x'},
        {'code': 'L1', 'endereco': 'SBC1', 'destinatario': 'x'},
        {'code': 'N1', 'endereco': 'PAD2', 'destinatario': 'x'},   # sobra perto da padaria
        {'code': 'N2', 'endereco': 'SBC2', 'destinatario': 'x'},   # sobra em Sao Bernardo
    ]
    coords = {'PAD1': (-23.600, -46.680), 'PAD2': (-23.601, -46.681),
              'SBC1': (-23.700, -46.550), 'SBC2': (-23.701, -46.551)}
    app.config['GOOGLE_MAPS_API_KEY'] = 'fake'
    with app.app_context(), \
            patch.object(svc.google_maps, 'geocode_em_lote',
                         side_effect=lambda ends: {e: coords[e] for e in ends}), \
            patch.object(svc.google_maps, 'geocode',
                         side_effect=lambda e: coords.get(e)), \
            patch.object(svc.google_maps, 'directions_otimizado',
                         return_value=None):
        r = svc.gerar_rotas(pedidos, drivers, atribuicoes=atribuicoes, app=app)
    por_driver = {rt['driver']['id']: sorted(p['code'] for p in rt['paradas'])
                  for rt in r['rotas']}
    # ANTES (por indice): cluster 0 -> Aaa, mesmo sendo a zona do William.
    # AGORA (ancora): sobra da padaria vai pro William; a de SBC pro Aaa.
    assert por_driver[2] == ['N1', 'W1']
    assert por_driver[1] == ['L1', 'N2']

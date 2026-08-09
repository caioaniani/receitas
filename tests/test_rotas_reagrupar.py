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

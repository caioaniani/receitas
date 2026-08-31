"""Snapshot persistente das vendas do Seru por dia (vendas_diarias): captura
idempotente a partir da API (mockada) + leitura do banco na mesma forma do
relatorio por loja."""
from datetime import date
from unittest.mock import patch

from app.models import VendaSeruDiaria
from app.services import vendas_diarias

DIA = date(2026, 6, 15)


def _pedido(pid, loja, itens, created='2026-06-15T13:00:00Z'):
    return {'id': pid, 'createdAt': created, 'canceledAt': None,
            'company': {'name': loja},
            'items': [{'name': n, 'quantity': q, 'total': t} for n, q, t in itens]}


PEDIDOS = [
    _pedido(1, 'Ribeiro do Vale', [('Cookie', 5, 50.0), ('Brioche', 2, 20.0)]),
    _pedido(2, 'Ribeiro do Vale', [('Cookie', 3, 30.0)]),
    _pedido(3, 'Nebraska', [('Cookie', 1, 10.0)]),
]


def _capturar(app, pedidos=PEDIDOS):
    with patch('app.services.seru.listar_pedidos_completo', return_value=pedidos):
        return vendas_diarias.capturar_periodo(DIA, DIA)


def test_captura_grava_por_dia_loja_produto(app):
    r = _capturar(app)
    assert r['pedidos'] == 3 and r['dias'] == 1
    # Ribeiro: Cookie (8 / R$80, 2 pedidos) + Brioche; Nebraska: Cookie (1)
    cookie_rib = VendaSeruDiaria.query.filter_by(
        loja_seru='Ribeiro do Vale', seru_nome='Cookie').first()
    assert cookie_rib is not None
    assert float(cookie_rib.qtd) == 8.0
    assert float(cookie_rib.faturamento) == 80.0
    assert cookie_rib.n_pedidos == 2
    assert VendaSeruDiaria.query.count() == 3   # 2 Ribeiro + 1 Nebraska


def test_captura_idempotente(app):
    _capturar(app)
    n1 = VendaSeruDiaria.query.count()
    _capturar(app)                               # recaptura o mesmo dia
    assert VendaSeruDiaria.query.count() == n1   # nao duplica


def test_captura_dia_todo_cancelado_zera(app):
    _capturar(app)
    assert VendaSeruDiaria.query.count() == 3
    # recaptura com tudo cancelado -> o dia zera (nada some pendurado)
    cancelado = [dict(p, canceledAt='2026-06-15T20:00:00Z') for p in PEDIDOS]
    with patch('app.services.seru.listar_pedidos_completo', return_value=cancelado):
        vendas_diarias.capturar_periodo(DIA, DIA)
    assert VendaSeruDiaria.query.count() == 0


def test_leitura_do_banco_mesma_forma_do_relatorio(app):
    _capturar(app)
    d = vendas_diarias.agregar_por_loja_do_banco(DIA, DIA)
    assert d['fonte'] == 'banco'
    assert d['faturamento_total'] == 110.0
    lojas = {lo['loja']: lo for lo in d['lojas']}
    assert set(lojas) == {'Ribeiro do Vale', 'Nebraska'}
    assert lojas['Ribeiro do Vale']['faturamento'] == 100.0
    cons = {p['nome']: p for p in d['consolidado']}
    assert cons['Cookie']['qtd'] == 9 and cons['Cookie']['faturamento'] == 90.0


def test_faturamento_por_loja_usa_total_do_pedido(app):
    """Kit/Box: itens vem com preco 0, dinheiro so no TOTAL do pedido. O
    faturamento (base do bot) usa o total — nao subconta pra 0."""
    ped = _pedido(9, 'Ribeiro do Vale', [('Box Mimo', 1, 0.0)])
    ped['total'] = 80.0
    with patch('app.services.seru.listar_pedidos_completo', return_value=[ped]):
        vendas_diarias.capturar_periodo(DIA, DIA)
    total, por_loja, n = vendas_diarias.faturamento_por_loja(DIA, DIA, capturar=False)
    assert total == 80.0
    assert por_loja['Ribeiro do Vale'] == 80.0
    assert n == 1


def test_agregar_flat_total_pedidos_nao_infla(app):
    """total_pedidos conta pedidos DISTINTOS (VendaSeruDiaLoja), nao soma por
    item — pedido com 2 itens nao vira 2."""
    _capturar(app)   # Ribeiro: ped1 (2 itens) + ped2 (1 item) = 2 pedidos
    d = vendas_diarias.agregar_flat(DIA, DIA, loja_seru='Ribeiro do Vale',
                                    capturar=False)
    assert d['total_pedidos'] == 2
    cookie = next(p for p in d['produtos'] if p['nome'] == 'Cookie')
    assert cookie['qtd'] == 8


def test_dias_capturados(app):
    _capturar(app)
    assert vendas_diarias.dias_capturados(DIA, DIA) == {DIA}
    assert vendas_diarias.dias_capturados(date(2026, 1, 1), date(2026, 1, 2)) == set()


@patch('app.services.vendas_diarias.capturar_periodo',
       return_value={'dias': 0, 'linhas': 0, 'pedidos': 0})
def test_rota_backfill_owner(_m, app, owner_user):
    """Owner dispara o backfill (background); nao-owner e bloqueado."""
    c = app.test_client()
    c.post('/auth/login', data={'login': owner_user.login, 'senha': '123'},
           follow_redirects=True)
    r = c.post('/pdv/vendas-diarias/backfill', data={'dias': '1'})
    assert r.status_code in (302, 303)


def test_rota_backfill_bloqueia_nao_owner(app, admin_user):
    c = app.test_client()
    c.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
           follow_redirects=True)
    r = c.post('/pdv/vendas-diarias/backfill', data={'dias': '1'},
               follow_redirects=False)
    assert r.status_code in (302, 403)


# ── Breakdowns da tela 'Vendas PDV' (pagamento/canal/cancelados) ──────────────

def _pedido_full(pid, loja, itens, *, total, payments, canal,
                 created='2026-06-15T13:00:00Z', canceled=None):
    return {'id': pid, 'createdAt': created, 'canceledAt': canceled,
            'company': {'name': loja}, 'total': total,
            'payments': payments, 'salesChannel': canal,
            'items': [{'name': n, 'quantity': q, 'total': t} for n, q, t in itens]}


PEDIDOS_FULL = [
    _pedido_full(1, 'Ribeiro do Vale', [('Cookie', 5, 50.0)],
                 total=50.0, payments=[{'method': 'dinheiro', 'value': 50.0}],
                 canal={'name': 'Balcão'}),
    _pedido_full(2, 'Nebraska', [('Cookie', 3, 30.0)],
                 total=30.0, payments=[{'method': 'pix', 'value': 30.0}],
                 canal={'name': 'Balcão'}),
    _pedido_full(3, 'Ribeiro do Vale', [('Cookie', 1, 10.0)],
                 total=10.0, payments=[], canal=None,
                 canceled='2026-06-15T20:00:00Z'),
]


def _capturar_full(app, pedidos=PEDIDOS_FULL):
    with patch('app.services.seru.listar_pedidos_completo', return_value=pedidos):
        return vendas_diarias.capturar_periodo(DIA, DIA)


def test_captura_grava_breakdowns(app):
    from app.models import VendaSeruDiaBreakdown
    _capturar_full(app)
    pag = {b.chave: float(b.valor) for b in
           VendaSeruDiaBreakdown.query.filter_by(dimensao='pagamento').all()}
    assert pag == {'dinheiro': 50.0, 'pix': 30.0}
    # Canal = soma do TOTAL do pedido (2 nao-cancelados, ambos Balcão) = 80.
    can = {}
    for b in VendaSeruDiaBreakdown.query.filter_by(dimensao='canal').all():
        can[b.chave] = can.get(b.chave, 0.0) + float(b.valor)
    assert can == {'Balcão': 80.0}
    # 1 pedido cancelado (Ribeiro): contagem (chave '') = 1, valor (chave 'v')
    # = total do pedido cancelado (10.0).
    canc_n = VendaSeruDiaBreakdown.query.filter_by(
        dimensao='cancelados', loja_seru='Ribeiro do Vale', chave='').first()
    assert canc_n is not None and int(canc_n.valor) == 1
    canc_v = VendaSeruDiaBreakdown.query.filter_by(
        dimensao='cancelados', loja_seru='Ribeiro do Vale', chave='v').first()
    assert canc_v is not None and float(canc_v.valor) == 10.0


def test_captura_conta_marketplaces_sem_misturar_dinheiro(app):
    from app.models import VendaSeruDiaBreakdown
    pedidos = [
        _pedido_full(11, 'Nebraska', [('Cookie', 1, 10.0)], total=10,
                     payments=[], canal={'name': 'iFood'}),
        _pedido_full(12, 'Nebraska', [], total=25, payments=[],
                     canal={'name': '99 Food', 'tag': '99food'}),
        _pedido_full(13, 'Ribeiro do Vale', [('Cookie', 2, 20.0)], total=20,
                     payments=[], canal={'name': 'Rappi', 'tag': 'rappi'}),
        _pedido_full(14, 'Ribeiro do Vale', [('Cookie', 1, 10.0)], total=10,
                     payments=[], canal={'name': 'iFood', 'tag': 'ifood'},
                     canceled='2026-06-15T20:00:00Z'),
    ]
    _capturar_full(app, pedidos)

    contagens = {}
    linhas = VendaSeruDiaBreakdown.query.filter_by(
        dimensao='marketplace').all()
    for linha in linhas:
        contagens[linha.chave] = (
            contagens.get(linha.chave, 0) + int(linha.valor))
    assert contagens == {'ifood': 1, '99food': 1, 'rappi': 1}


def test_captura_breakdown_idempotente(app):
    from app.models import VendaSeruDiaBreakdown
    _capturar_full(app)
    n1 = VendaSeruDiaBreakdown.query.count()
    _capturar_full(app)
    assert VendaSeruDiaBreakdown.query.count() == n1   # nao duplica


def test_vendas_pdv_do_banco(app):
    _capturar_full(app)
    d = vendas_diarias.vendas_pdv_do_banco(DIA, DIA, capturar=False)
    assert d['fonte'] == 'banco'
    assert d['total_valor'] == 80.0        # 50 + 30 (cancelado fora)
    assert d['n_pedidos'] == 2
    assert d['cancelados'] == 1
    assert d['por_pagamento'] == {'dinheiro': 50.0, 'pix': 30.0}
    assert d['por_canal'] == {'Balcão': 80.0}
    assert d['por_loja'] == {'Ribeiro do Vale': 50.0, 'Nebraska': 30.0}
    det = d['por_loja_detalhe']
    assert det['Ribeiro do Vale']['total'] == 50.0
    assert det['Ribeiro do Vale']['n_pedidos'] == 1
    assert det['Ribeiro do Vale']['cancelados'] == 1
    assert det['Ribeiro do Vale']['por_pagamento'] == {'dinheiro': 50.0}
    assert det['Nebraska']['cancelados'] == 0
    # valor do cancelado (chave 'v') = total do pedido cancelado (10.0);
    # sem desconto no fixture.
    assert d['cancelados_valor'] == 10.0
    assert det['Ribeiro do Vale']['cancelados_valor'] == 10.0
    assert d['desconto'] == 0.0
    # sem cobrança só-valor no fixture: split zerado/ausente
    assert d['sem_itens_total'] == 0.0
    assert d['sem_itens_n'] == 0
    assert d['por_loja_sem_itens'] == {}


def test_captura_separa_cobranca_sem_itens(app):
    """Caso Nebraska 17/07/2026 (teste de impressora no PDV Fácil): cobrança
    com valor e ZERO itens vira dimensão 'sem_itens' no breakdown — o card
    Por loja mostra a venda COM produto na linha e isso no rodapé."""
    pedidos = PEDIDOS_FULL + [
        _pedido_full(9, 'Nebraska', [], total=1135.0,
                     payments=[], canal={'name': 'PDV Fácil'}),
        _pedido_full(10, 'Nebraska', [], total=578.0,
                     payments=[], canal={'name': 'PDV Fácil'}),
        # cancelada e zero-valor NÃO entram no rodapé
        _pedido_full(11, 'Nebraska', [], total=99.0, payments=[],
                     canal={'name': 'PDV Fácil'},
                     canceled='2026-06-15T20:00:00Z'),
        _pedido_full(12, 'Nebraska', [], total=0.0, payments=[],
                     canal={'name': 'PDV Fácil'}),
    ]
    _capturar_full(app, pedidos)
    d = vendas_diarias.vendas_pdv_do_banco(DIA, DIA, capturar=False)
    assert d['por_loja_sem_itens'] == {'Nebraska': 1713.0}
    assert d['sem_itens_total'] == 1713.0
    # CONTAGEM também (o resumo desconta pedidos e ticket — dono 18/07):
    # 2 cobranças válidas (cancelada e zero-valor fora)
    assert d['sem_itens_n'] == 2
    assert d['por_loja_sem_itens_n'] == {'Nebraska': 2}
    # total cheio da loja segue incluindo tudo (semântica de sempre);
    # a SEPARAÇÃO é papel do front (linha = total - sem_itens)
    assert d['por_loja']['Nebraska'] == 1743.0            # 30 + 1135 + 578
    assert d['por_loja_detalhe']['Nebraska']['sem_itens'] == 1713.0
    assert d['por_loja_detalhe']['Nebraska']['sem_itens_n'] == 2


def test_snapshot_antigo_sem_dimensao_fica_zerado(app):
    """Dia capturado ANTES da dimensão existir (sem linha 'sem_itens' no
    breakdown): split fica 0 e o card mostra o total cheio, como era."""
    from app.extensions import db
    from app.models import VendaSeruDiaBreakdown
    _capturar_full(app)
    VendaSeruDiaBreakdown.query.filter_by(dimensao='sem_itens').delete()
    db.session.commit()
    d = vendas_diarias.vendas_pdv_do_banco(DIA, DIA, capturar=False)
    assert d['sem_itens_total'] == 0.0 and d['por_loja_sem_itens'] == {}


def test_rota_api_vendas_le_do_banco(app, admin_user):
    """A tela 'Vendas PDV' (default) le do snapshot: fonte=banco, sem pedidos
    crus, com por_loja_detalhe pro filtro."""
    _capturar_full(app)   # DIA e passado -> garantir_capturado nao rebate na API
    c = app.test_client()
    c.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
           follow_redirects=True)
    r = c.get('/pdv/api/vendas?inicio=2026-06-15&fim=2026-06-15')
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] and j['fonte'] == 'banco'
    assert j['total_valor'] == 80.0
    assert j['cancelados'] == 1
    assert j['pedidos'] is None
    assert j['por_loja_detalhe']['Ribeiro do Vale']['total'] == 50.0


def test_rota_api_vendas_ao_vivo_traz_pedidos(app, admin_user):
    """?ao_vivo=1 volta a consultar a API e traz o detalhe pedido-a-pedido."""
    c = app.test_client()
    c.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
           follow_redirects=True)
    with patch('app.services.seru.listar_pedidos_completo',
               return_value=PEDIDOS_FULL):
        r = c.get('/pdv/api/vendas?inicio=2026-06-15&fim=2026-06-15&ao_vivo=1')
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] and j['fonte'] == 'ao_vivo'
    assert isinstance(j['pedidos'], list) and len(j['pedidos']) == 3


def test_captura_delivery_sem_itens_separado_e_cancelado_por_status(app):
    """Dono 18/07: 99Food (delivery sem itens) é venda REAL — bucket
    separado que CONTA no faturamento; cancelado por status (sem
    canceledAt) sai de tudo."""
    p99 = _pedido_full(20, 'Anesio', [], total=81.38, payments=[],
                       canal={'name': '99Food', 'tag': '99food'})
    avulsa = _pedido_full(21, 'Anesio', [], total=44.0, payments=[],
                          canal={'name': 'PDV Fácil', 'tag': 'pdv-facil'})
    canc = _pedido_full(22, 'Anesio', [('Pao', 1, 30.0)], total=30.0,
                        payments=[], canal={'name': 'Balcão'})
    canc['status'] = 'canceled'                    # sem canceledAt!
    _capturar_full(app, PEDIDOS_FULL + [p99, avulsa, canc])
    d = vendas_diarias.vendas_pdv_do_banco(DIA, DIA, capturar=False)
    # avulsa fora do faturamento; delivery em bucket informativo
    assert d['sem_itens_total'] == 44.0 and d['sem_itens_n'] == 1
    assert d['delivery_sem_itens_total'] == 81.38
    assert d['por_loja_delivery_sem_itens'] == {'Anesio': 81.38}
    # cancelado por status não vira venda (nem os R$30 no total da loja)
    assert d['por_loja']['Anesio'] == 125.38       # 81.38 + 44.00
    assert d['por_loja_detalhe']['Anesio']['cancelados'] == 1


# ── Cancelamentos (valor) e descontos do dia — cockpit da home ────────────────

def _pedido_desc(pid, loja, total, discount, *, canceled=None):
    """Pedido com `discount` (top-level da API Seru, R$)."""
    return {'id': pid, 'createdAt': '2026-06-15T13:00:00Z',
            'canceledAt': canceled, 'company': {'name': loja}, 'total': total,
            'discount': discount, 'payments': [], 'salesChannel': None,
            'items': [{'name': 'Pao', 'quantity': 1, 'total': total}]}


PEDIDOS_DESC = [
    _pedido_desc(1, 'Ribeiro do Vale', 50.0, 10.0),            # desconto conta
    _pedido_desc(2, 'Nebraska', 30.0, 0.0),                    # sem desconto
    _pedido_desc(3, 'Ribeiro do Vale', 10.0, 5.0,              # CANCELADO:
                 canceled='2026-06-15T20:00:00Z'),             # desconto ignorado
]


def test_captura_grava_desconto_e_valor_cancelado(app):
    from app.models import VendaSeruDiaBreakdown
    with patch('app.services.seru.listar_pedidos_completo',
               return_value=PEDIDOS_DESC):
        vendas_diarias.capturar_periodo(DIA, DIA)
    # desconto: só o pedido 1 (não cancelado) — 10.0 na Ribeiro; nada na Nebraska.
    desc = {b.loja_seru: float(b.valor) for b in
            VendaSeruDiaBreakdown.query.filter_by(dimensao='desconto').all()}
    assert desc == {'Ribeiro do Vale': 10.0}
    # cancelados: contagem 1 (chave '') + valor 10.0 (chave 'v', total do
    # pedido 3), ambos na Ribeiro.
    cn = VendaSeruDiaBreakdown.query.filter_by(
        dimensao='cancelados', chave='', loja_seru='Ribeiro do Vale').first()
    cv = VendaSeruDiaBreakdown.query.filter_by(
        dimensao='cancelados', chave='v', loja_seru='Ribeiro do Vale').first()
    assert int(cn.valor) == 1 and float(cv.valor) == 10.0


def test_helper_cancelamentos_descontos_do_banco(app):
    with patch('app.services.seru.listar_pedidos_completo',
               return_value=PEDIDOS_DESC):
        vendas_diarias.capturar_periodo(DIA, DIA)
    cd = vendas_diarias.cancelamentos_descontos_do_banco(DIA, DIA)
    assert cd == {'cancelados_n': 1, 'cancelados_valor': 10.0, 'desconto': 10.0}


def test_desconto_reader_vendas_pdv_do_banco(app):
    with patch('app.services.seru.listar_pedidos_completo',
               return_value=PEDIDOS_DESC):
        vendas_diarias.capturar_periodo(DIA, DIA)
    d = vendas_diarias.vendas_pdv_do_banco(DIA, DIA, capturar=False)
    assert d['desconto'] == 10.0
    assert d['cancelados'] == 1 and d['cancelados_valor'] == 10.0
    assert d['por_loja_detalhe']['Ribeiro do Vale']['desconto'] == 10.0


def test_snapshot_antigo_sem_desconto_nem_valor_fica_zerado(app):
    """Dia capturado ANTES das linhas 'desconto'/'cancelados v' existirem: o
    helper devolve 0 no que falta (contagem de cancelados segue), sem quebrar —
    e o valor do cancelado nunca é lido como contagem."""
    from app.extensions import db
    from app.models import VendaSeruDiaBreakdown
    with patch('app.services.seru.listar_pedidos_completo',
               return_value=PEDIDOS_DESC):
        vendas_diarias.capturar_periodo(DIA, DIA)
    # simula snapshot velho: remove a dimensão desconto e a chave 'v' do cancelado
    VendaSeruDiaBreakdown.query.filter_by(dimensao='desconto').delete()
    VendaSeruDiaBreakdown.query.filter_by(
        dimensao='cancelados', chave='v').delete()
    db.session.commit()
    cd = vendas_diarias.cancelamentos_descontos_do_banco(DIA, DIA)
    assert cd == {'cancelados_n': 1, 'cancelados_valor': 0.0, 'desconto': 0.0}
    d = vendas_diarias.vendas_pdv_do_banco(DIA, DIA, capturar=False)
    assert d['cancelados'] == 1 and d['cancelados_valor'] == 0.0

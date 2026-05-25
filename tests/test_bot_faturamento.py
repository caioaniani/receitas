"""Testes do endpoint /api/bot/faturamento (o atalho do celular) + helper VNDA.

Cobre:
- soma PDV (Seru) + site (VNDA) no mesmo total;
- site indisponivel NAO derruba o endpoint (200 com aviso, nao 502);
- faturamento VNDA por DATA DE VENDA (confirmed_at UTC -> BRT), ignorando
  pedido cancelado, fora do intervalo, e usando fallback price*quantity.
"""
from datetime import date
from unittest.mock import patch

TOKEN = 'tok-teste'


def _cfg(app):
    app.config['BOT_API_TOKEN'] = TOKEN
    app.config['BOT_ALLOWED_PHONES'] = ''  # vazio = sem whitelist de telefone


def _seru_pedido(pid, company, total, created_at='2026-05-20T13:00:00Z'):
    return {'id': pid, 'createdAt': created_at, 'canceledAt': None,
            'company': {'name': company}, 'total': total, 'items': []}


def _loja_vnda(app):
    from app.extensions import db
    from app.models import Loja
    loja = Loja(nome='Loja Anesio Pinto Rosa', ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


def _vnda_order(code, status, confirmed_at, itens):
    return {'code': code, 'status': status, 'confirmed_at': confirmed_at,
            'items': itens}


# ── Endpoint ───────────────────────────────────────────────────────────

def test_endpoint_soma_pdv_e_site(app):
    _cfg(app)
    _loja_vnda(app)
    seru_pedidos = [_seru_pedido(1, 'Ribeiro do Vale', 100.0),
                    _seru_pedido(2, 'Nebraska', 50.0)]
    fat_site = {'total': 30.0, 'n_pedidos': 2,
                'por_dia': {date(2026, 5, 20): 30.0}}
    with patch('app.services.seru.listar_pedidos_completo', return_value=seru_pedidos), \
         patch('app.services.vnda_sync.faturamento_por_dia', return_value=fat_site):
        resp = app.test_client().get(
            f'/api/bot/faturamento?token={TOKEN}&data=2026-05-20')
    assert resp.status_code == 200
    j = resp.get_json()
    assert j['ok'] is True
    assert j['total'] == 180.0
    assert j['total_pdv'] == 150.0
    assert j['total_site'] == 30.0
    assert j['qtd_pedidos'] == 4  # 2 PDV + 2 site
    assert j['por_loja']['Loja Anesio Pinto Rosa (site)'] == 30.0
    assert 'site' in j['mensagem'].lower()


def test_endpoint_site_indisponivel_nao_derruba(app):
    _cfg(app)
    from app.services import vnda
    seru_pedidos = [_seru_pedido(1, 'Ribeiro do Vale', 100.0)]
    with patch('app.services.seru.listar_pedidos_completo', return_value=seru_pedidos), \
         patch('app.services.vnda_sync.faturamento_por_dia',
               side_effect=vnda.VndaUnavailableError('site fora')):
        resp = app.test_client().get(
            f'/api/bot/faturamento?token={TOKEN}&data=2026-05-20')
    assert resp.status_code == 200  # NAO 502 — VNDA eh best-effort
    j = resp.get_json()
    assert j['total'] == 100.0
    assert j['total_site'] == 0.0
    assert 'indispon' in j['mensagem'].lower()


def test_endpoint_token_invalido(app):
    _cfg(app)
    resp = app.test_client().get(
        '/api/bot/faturamento?token=errado&data=2026-05-20')
    assert resp.status_code == 401


# ── Helper faturamento_por_dia ─────────────────────────────────────────

def test_helper_faturamento_por_dia(app):
    from app.services import vnda_sync
    orders = [
        # conta: 20 + 10 = 30
        _vnda_order('A', 'paid', '2026-05-20T13:00:00Z',
                    [{'subtotal': 20.0}, {'subtotal': 10.0}]),
        # cancelado -> ignora
        _vnda_order('B', 'canceled', '2026-05-20T13:00:00Z',
                    [{'subtotal': 99.0}]),
        # fora do intervalo (dia 18) -> ignora
        _vnda_order('C', 'paid', '2026-05-18T13:00:00Z',
                    [{'subtotal': 77.0}]),
        # sem subtotal -> fallback price*quantity = 10
        _vnda_order('D', 'paid', '2026-05-20T15:00:00Z',
                    [{'price': 5.0, 'quantity': 2}]),
        # 01h UTC = 22h BRT do dia 20 -> conta no dia 20 (prova UTC->BRT)
        _vnda_order('E', 'paid', '2026-05-21T01:00:00Z',
                    [{'subtotal': 5.0}]),
    ]
    with patch('app.services.vnda._buscar_pedidos_janela', return_value=orders):
        r = vnda_sync.faturamento_por_dia(date(2026, 5, 20), date(2026, 5, 20))
    assert r['total'] == 45.0  # 30 + 10 + 5
    assert r['n_pedidos'] == 3
    assert r['por_dia'][date(2026, 5, 20)] == 45.0

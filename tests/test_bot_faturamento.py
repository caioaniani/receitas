"""Testes do endpoint /api/bot/faturamento (o atalho do celular) + helper VNDA.

Cobre:
- soma PDV (Seru) + site (loja propria / PedidoOnline) no mesmo total;
- site indisponivel NAO derruba o endpoint (200 com aviso, nao 502);
- faturamento do site por DATA DE VENDA (VNDA aposentado 24/06 e vnda_sync
  REMOVIDO: a fonte do site agora e PedidoOnline / loja propria).
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


# ── Endpoint ───────────────────────────────────────────────────────────

def test_endpoint_soma_pdv_e_site(app):
    _cfg(app)
    seru_pedidos = [_seru_pedido(1, 'Ribeiro do Vale', 100.0),
                    _seru_pedido(2, 'Nebraska', 50.0)]
    # Site = loja propria (PedidoOnline), NAO VNDA (aposentado).
    fat_site = {'total': 30.0, 'n_pedidos': 2,
                'por_dia': {date(2026, 5, 20): 30.0}}
    with patch('app.services.seru.listar_pedidos_completo', return_value=seru_pedidos), \
         patch('app.services.loja_online_vendas.faturamento_por_dia', return_value=fat_site):
        resp = app.test_client().get(
            f'/api/bot/faturamento?token={TOKEN}&data=2026-05-20')
    assert resp.status_code == 200
    j = resp.get_json()
    assert j['ok'] is True
    assert j['total'] == 180.0
    assert j['total_pdv'] == 150.0
    assert j['total_site'] == 30.0
    assert j['qtd_pedidos'] == 4  # 2 PDV + 2 site
    assert j['por_loja']['Site'] == 30.0
    assert 'site' in j['mensagem'].lower()


def test_endpoint_site_indisponivel_nao_derruba(app):
    _cfg(app)
    seru_pedidos = [_seru_pedido(1, 'Ribeiro do Vale', 100.0)]
    with patch('app.services.seru.listar_pedidos_completo', return_value=seru_pedidos), \
         patch('app.services.loja_online_vendas.faturamento_por_dia',
               side_effect=RuntimeError('site fora')):
        resp = app.test_client().get(
            f'/api/bot/faturamento?token={TOKEN}&data=2026-05-20')
    assert resp.status_code == 200  # NAO 502 — site eh best-effort
    j = resp.get_json()
    assert j['total'] == 100.0
    assert j['total_site'] == 0.0
    assert 'indispon' in j['mensagem'].lower()


def test_endpoint_token_invalido(app):
    _cfg(app)
    resp = app.test_client().get(
        '/api/bot/faturamento?token=errado&data=2026-05-20')
    assert resp.status_code == 401

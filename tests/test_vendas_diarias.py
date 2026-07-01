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

"""Copilot/vendas: inclusao da loja propria (PedidoOnline) + VNDA historico.

Desde o cutover (22/06/2026) a fonte das vendas do site e o `PedidoOnline`
(VNDA desligado). Cobre:
- `vendas_vnda_loja`: monta a venda do site (loja propria) no formato do copilot.
- `agregar_itens_consolidado`: faturamento total = Seru + VNDA(historico) + site.
- `_read_consultar_vendas_itens`: filtro pela loja do site (Anesio) usa a loja
  propria.
"""
from datetime import date
from unittest.mock import patch


def test_vendas_site_loja_formato(app):
    from app.services import vendas_itens
    vd = {
        'produtos': [
            {'nome': 'Croissant', 'tipo': 'receita', 'id': 9, 'qtd': 7},
        ],
        'total_pedidos': 4,
    }
    with patch('app.services.loja_online_vendas.produtos_vendidos',
               return_value=vd), \
         patch('app.services.loja_online_vendas.faturamento_por_dia',
               return_value={'total': 123.45, 'n_pedidos': 4, 'por_dia': {}}):
        r = vendas_itens.vendas_vnda_loja(date(2026, 5, 20), date(2026, 5, 20))
    assert r['faturamento_total'] == 123.45
    assert r['faturamento_fonte'] == 'site'
    assert r['total_pedidos'] == 4
    p0 = r['produtos'][0]
    assert p0['fonte'] == 'site'
    assert p0['qtd_online'] == 7 and p0['qtd_seru'] == 0
    assert p0['match']['nome'] == 'Croissant'


def test_vendas_site_loja_vazio(app):
    from app.services import vendas_itens
    with patch('app.services.loja_online_vendas.produtos_vendidos',
               return_value={'produtos': [], 'total_pedidos': 0}), \
         patch('app.services.loja_online_vendas.faturamento_por_dia',
               return_value={'total': 0.0, 'n_pedidos': 0, 'por_dia': {}}):
        r = vendas_itens.vendas_vnda_loja(date(2026, 5, 20), date(2026, 5, 20))
    assert r['produtos'] == []
    assert r['faturamento_total'] == 0.0


def test_consolidado_soma_seru_vnda_e_site(app):
    from app.services import vendas_itens
    seru_data = {
        'inicio': '2026-05-20', 'fim': '2026-05-20', 'loja': None,
        'total_pedidos': 5, 'total_itens_vendidos': 10,
        'faturamento_total': 200.0, 'produtos': [], 'sem_match_count': 0,
        'pendentes_count': 0, 'lojas_no_intervalo': ['Ribeiro do Vale'],
    }
    with patch('app.services.vendas_itens.agregar_itens',
               return_value=seru_data), \
         patch('app.services.vendas_manuais._agregar_vendas_vnda_api',
               return_value=({}, None)), \
         patch('app.services.vnda_sync.faturamento_por_dia',
               return_value={'total': 55.0, 'n_pedidos': 2, 'por_dia': {}}), \
         patch('app.services.loja_online_vendas.vendas_por_produto',
               return_value={}), \
         patch('app.services.loja_online_vendas.faturamento_por_dia',
               return_value={'total': 30.0, 'n_pedidos': 1, 'por_dia': {}}):
        r = vendas_itens.agregar_itens_consolidado(
            date(2026, 5, 20), date(2026, 5, 20))
    assert r['faturamento_total'] == 285.0   # 200 Seru + 55 VNDA + 30 site
    assert r['faturamento_seru'] == 200.0
    assert r['faturamento_vnda'] == 55.0
    assert r['faturamento_online'] == 30.0
    assert r['faturamento_fonte'] == 'seru+site'


def test_copilot_loja_anesio_roteia_pra_site(app):
    from app.extensions import db
    from app.models import Loja
    db.session.add(Loja(nome='Loja Anesio Pinto Rosa', ativa=True))
    db.session.commit()

    from app.services import copilot
    fake = {
        'inicio': '2026-05-20', 'fim': '2026-05-20', 'total_pedidos': 3,
        'faturamento_total': 88.0, 'faturamento_fonte': 'site',
        'produtos': [{'nome': 'Croissant', 'qtd': 5, 'qtd_seru': 0,
                      'qtd_vnda': 0, 'qtd_online': 5, 'faturamento': 0,
                      'fonte': 'site', 'match': None}],
        'vnda_aviso': None, 'lojas_no_intervalo': ['Loja Anesio Pinto Rosa'],
    }
    with patch('app.services.vendas_itens.vendas_vnda_loja',
               return_value=fake) as m:
        res = copilot._read_consultar_vendas_itens(
            {'loja': 'Anesio', 'inicio': '2026-05-20', 'fim': '2026-05-20'}, None)
    assert m.called
    assert 'site' in res['texto'].lower()
    assert 'R$ 88.00' in res['texto']

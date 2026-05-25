"""Copilot/vendas: inclusao do VNDA (quantidade + faturamento).

- `vendas_vnda_loja`: monta a venda do site no formato do copilot.
- `agregar_itens_consolidado`: faturamento total passa a somar VNDA.
- `_read_consultar_vendas_itens`: filtro pela loja do site (Anesio) usa VNDA.
"""
from datetime import date
from unittest.mock import patch


def test_vendas_vnda_loja_formato(app):
    from app.services import vendas_itens
    vd = {
        'produtos': [
            {'nome': 'Croissant', 'sku': 'CRO', 'qtd': 7, 'n_pedidos': 3,
             'estado_map': 'mapeado',
             'mapeado_para': {'tipo': 'receita', 'id': 9, 'nome': 'Croissant'},
             'fator': 1.0},
            {'nome': 'Item Sem Map', 'sku': None, 'qtd': 2, 'n_pedidos': 1,
             'estado_map': 'sem_map', 'mapeado_para': None, 'fator': 1.0},
        ],
        'total_pedidos': 4, 'total_itens': 9, 'loja': 'Loja Anesio Pinto Rosa',
    }
    with patch('app.services.vnda_sync.agregar_vendas', return_value=vd), \
         patch('app.services.vnda_sync.faturamento_por_dia',
               return_value={'total': 123.45, 'n_pedidos': 4, 'por_dia': {}}):
        r = vendas_itens.vendas_vnda_loja(date(2026, 5, 20), date(2026, 5, 20))
    assert r['faturamento_total'] == 123.45
    assert r['faturamento_fonte'] == 'vnda'
    assert r['total_pedidos'] == 4
    assert r['lojas_no_intervalo'] == ['Loja Anesio Pinto Rosa']
    p0 = r['produtos'][0]
    assert p0['fonte'] == 'vnda'
    assert p0['qtd_vnda'] == 7 and p0['qtd_seru'] == 0
    assert p0['match']['nome'] == 'Croissant'
    assert r['produtos'][1]['match'] is None


def test_vendas_vnda_loja_site_fora(app):
    from app.services import vendas_itens
    with patch('app.services.vnda_sync.agregar_vendas',
               return_value={'erro': 'vnda_indisponivel: timeout'}):
        r = vendas_itens.vendas_vnda_loja(date(2026, 5, 20), date(2026, 5, 20))
    assert r['produtos'] == []
    assert r['faturamento_total'] == 0.0
    assert 'indispon' in (r['vnda_aviso'] or '').lower()


def test_consolidado_inclui_faturamento_vnda(app):
    from app.services import vendas_itens
    seru_data = {
        'inicio': '2026-05-20', 'fim': '2026-05-20', 'loja': None,
        'total_pedidos': 5, 'total_itens_vendidos': 10,
        'faturamento_total': 200.0, 'produtos': [], 'sem_match_count': 0,
        'pendentes_count': 0, 'lojas_no_intervalo': ['Ribeiro do Vale'],
    }
    with patch('app.services.vendas_itens.agregar_itens', return_value=seru_data), \
         patch('app.services.vendas_manuais._agregar_vendas_vnda_api',
               return_value=({}, None)), \
         patch('app.services.vnda_sync.faturamento_por_dia',
               return_value={'total': 55.0, 'n_pedidos': 2, 'por_dia': {}}):
        r = vendas_itens.agregar_itens_consolidado(
            date(2026, 5, 20), date(2026, 5, 20))
    assert r['faturamento_total'] == 255.0
    assert r['faturamento_seru'] == 200.0
    assert r['faturamento_vnda'] == 55.0
    assert r['faturamento_fonte'] == 'seru+vnda'


def test_copilot_loja_anesio_roteia_pra_vnda(app):
    from app.extensions import db
    from app.models import Loja
    db.session.add(Loja(nome='Loja Anesio Pinto Rosa', ativa=True))
    db.session.commit()

    from app.services import copilot
    fake = {
        'inicio': '2026-05-20', 'fim': '2026-05-20', 'total_pedidos': 3,
        'faturamento_total': 88.0, 'faturamento_fonte': 'vnda',
        'produtos': [{'nome': 'Croissant', 'qtd': 5, 'qtd_seru': 0,
                      'qtd_vnda': 5, 'faturamento': 0, 'fonte': 'vnda',
                      'match': None}],
        'vnda_aviso': None, 'lojas_no_intervalo': ['Loja Anesio Pinto Rosa'],
    }
    with patch('app.services.vendas_itens.vendas_vnda_loja',
               return_value=fake) as m:
        res = copilot._read_consultar_vendas_itens(
            {'loja': 'Anesio', 'inicio': '2026-05-20', 'fim': '2026-05-20'}, None)
    assert m.called
    assert 'VNDA' in res['texto']
    assert 'R$ 88.00' in res['texto']

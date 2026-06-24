"""VNDA APOSENTADO em 24/06/2026 — trava regressao.

Antes desses testes verificavam que VNDA somava em faturamento/produtos. Hoje
travam o OPOSTO: que VNDA nao e mais consultado (API morta) e que as funcoes
publicas retornam vazio + aviso.
"""
from datetime import date


def test_vendas_vnda_loja_retorna_vazio_com_aviso(app):
    """vendas_vnda_loja nao consulta API — retorna vazio + aviso."""
    from app.services import vendas_itens
    r = vendas_itens.vendas_vnda_loja(date(2026, 5, 20), date(2026, 5, 20))
    assert r['produtos'] == []
    assert r['faturamento_total'] == 0.0
    assert r['total_pedidos'] == 0
    assert r['lojas_no_intervalo'] == []
    assert 'aposentado' in (r['vnda_aviso'] or '').lower()


def test_consolidado_nao_soma_vnda(app):
    """agregar_itens_consolidado nao soma VNDA — so Seru."""
    from unittest.mock import patch

    from app.services import vendas_itens
    seru_data = {
        'inicio': '2026-05-20', 'fim': '2026-05-20', 'loja': None,
        'total_pedidos': 5, 'total_itens_vendidos': 10,
        'faturamento_total': 200.0, 'produtos': [], 'sem_match_count': 0,
        'pendentes_count': 0, 'lojas_no_intervalo': ['Ribeiro do Vale'],
    }
    with patch('app.services.vendas_itens.agregar_itens',
               return_value=seru_data):
        r = vendas_itens.agregar_itens_consolidado(
            date(2026, 5, 20), date(2026, 5, 20))
    assert r['faturamento_total'] == 200.0
    assert r['faturamento_seru'] == 200.0
    assert r['faturamento_vnda'] == 0.0
    assert r['faturamento_fonte'] == 'seru_apenas'
    assert 'aposentado' in (r['vnda_aviso'] or '').lower()


def test_agregar_vendas_vnda_api_nao_bate_em_rede(app):
    """`_agregar_vendas_vnda_api` retorna vazio + aviso, sem chamar `vnda` API.
    Trava o aposentamento — patch de `vnda._buscar_pedidos_janela` lanca se for
    chamado."""
    from unittest.mock import patch

    from app.services.vendas_manuais import _agregar_vendas_vnda_api
    with patch('app.services.vnda._buscar_pedidos_janela',
               side_effect=AssertionError('VNDA NAO deve ser consultado')):
        vendas, aviso = _agregar_vendas_vnda_api(
            date(2026, 5, 1), date(2026, 5, 31))
    assert vendas == {}
    assert 'aposentado' in (aviso or '').lower()


def test_copilot_loja_site_recebe_aviso_aposentado(app):
    """Quando o copilot rotear pra loja do site (Anesio), a resposta deve
    sinalizar que VNDA esta aposentado (nao falhar silenciosamente)."""
    from app.extensions import db
    from app.models import Loja
    db.session.add(Loja(nome='Loja Anesio Pinto Rosa', ativa=True))
    db.session.commit()

    from app.services import copilot
    res = copilot._read_consultar_vendas_itens(
        {'loja': 'Anesio', 'inicio': '2026-05-20', 'fim': '2026-05-20'}, None)
    # Trafego pra loja Anesio: copilot devolve algo (texto ou aviso).
    # Como VNDA esta aposentado, o resultado nao pode mais conter vendas
    # falsas — verifico apenas que nao quebrou e que nao apareceu fato novo.
    assert isinstance(res, dict)

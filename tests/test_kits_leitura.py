"""Leitura em lote reduz consultas sem conservar preço ou saldo entre pedidos."""
import json
import re
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event
from test_kits_adicionais import extra as extra
from test_kits_checkout import BASE, _abrir, _post
from test_kits_checkout import ambiente as ambiente
from test_kits_checkout import frete as frete
from test_kits_checkout import kit as kit

from app.extensions import db
from app.models import (
    EstoqueSiteExcecao,
    EstoqueSitePlano,
    EstoqueSiteRegraSemanal,
    LojaDataEspecial,
    Produto,
)
from app.services import compra_kits, loja_catalogo, loja_checkout, loja_leitura, loja_plano_dia

pytestmark = pytest.mark.loja_host


@pytest.fixture(autouse=True)
def calendario(congela_hoje):
    congela_hoje(2026, 9, 14, 6)


def _dia(indice):
    return BASE.date() + timedelta(days=indice)


def _config(html):
    bruto = re.search(r'<script id="kits-config"[^>]*>(.*?)</script>', html, re.S)
    assert bruto is not None
    return json.loads(bruto[1])


def _ofertas(html):
    return {(item['kind'], item['id']): item for item in _config(html)['adicionais']}


def test_lote_preserva_saldos_reservas_excecoes_semana_e_ausencia_de_plano(app):
    itens = [Produto(nome=f'Produto {i}', ativo=True, site_ativo=True, preco_site=10)
             for i in range(4)]
    db.session.add_all(itens)
    db.session.flush()
    _, diario, semanal, ilimitado = itens
    db.session.add_all([
        EstoqueSitePlano(kind='produto', item_id=diario.id, data=_dia(1),
                         qtd_planejada=10, qtd_reservada=3),
        EstoqueSitePlano(kind='produto', item_id=semanal.id, data=_dia(1),
                         qtd_planejada=50, qtd_reservada=2),
        EstoqueSiteRegraSemanal(kind='produto', item_id=semanal.id,
                                dias_mask=1 << _dia(1).weekday(), qtd_limite=8),
        EstoqueSiteExcecao(kind='produto', item_id=semanal.id,
                           data=_dia(3), qtd_limite=4),
        EstoqueSiteRegraSemanal(kind='produto', item_id=ilimitado.id,
                                dias_mask=127, qtd_limite=None),
        EstoqueSitePlano(kind='produto', item_id=ilimitado.id, data=_dia(1),
                         qtd_planejada=7, qtd_reservada=2),
        EstoqueSiteExcecao(kind='produto', item_id=ilimitado.id,
                           data=_dia(2), qtd_limite=0),
    ])
    db.session.commit()
    esperado = {(item.id, dia): loja_plano_dia.saldo('produto', item.id, _dia(dia))
                for item in itens for dia in range(4)}
    assert esperado[(itens[0].id, 1)] is None
    assert esperado[(diario.id, 1)] == 7
    assert esperado[(semanal.id, 1)] == 6
    assert esperado[(semanal.id, 2)] == 0
    assert esperado[(semanal.id, 3)] == 4
    assert esperado[(ilimitado.id, 2)] == 0
    with loja_leitura.catalogo_em_lote(base=BASE):
        for (item_id, dia), saldo in esperado.items():
            assert loja_catalogo._saldo_para_dia('produto', item_id, _dia(dia)) == saldo
    assert loja_leitura.atual() is None


def test_consultas_de_venda_em_lote_nao_repetem_select_por_produto(app):
    produtos = [Produto(nome=f'Avulso {i}', ativo=True, site_ativo=True,
                         preco_site=Decimal('10.50')) for i in range(20)]
    db.session.add_all(produtos)
    db.session.commit()
    ids = [produto.id for produto in produtos]
    selects = []

    def registrar(_conn, _cursor, statement, _params, _context, _executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            selects.append(statement)

    with loja_leitura.catalogo_em_lote(base=BASE):
        event.listen(db.engine, 'before_cursor_execute', registrar)
        try:
            for _ in range(3):
                itens, erros = loja_checkout.montar_itens(
                    [{'kind': 'produto', 'id': item_id, 'qtd': 2} for item_id in ids],
                    dias_disponibilidade=31, base=BASE)
                assert not erros and len(itens) == 20
                assert sum(item['subtotal'] for item in itens) == Decimal('420.00')
        finally:
            event.remove(db.engine, 'before_cursor_execute', registrar)
    assert not selects, selects


def test_lookup_fora_do_escopo_volta_a_publicacao_e_preco_atuais(extra):
    with loja_leitura.catalogo_em_lote(base=BASE):
        assert loja_catalogo.por_id_venda('produto', extra.id)['preco'] == 7.35
    extra.preco_site = Decimal('19.25')
    db.session.commit()
    assert loja_leitura.atual() is None
    assert loja_catalogo.por_id_venda('produto', extra.id)['preco'] == 19.25
    extra.site_ativo = False
    db.session.commit()
    assert loja_catalogo.por_id_venda('produto', extra.id) is None


def test_excecao_descarta_lote_e_nao_esconde_nova_publicacao(extra):
    with pytest.raises(RuntimeError, match='falha no render'):
        with loja_leitura.catalogo_em_lote(base=BASE):
            assert loja_leitura.atual() is not None
            raise RuntimeError('falha no render')
    assert loja_leitura.atual() is None
    extra.preco_site = Decimal('18.50')
    db.session.commit()
    assert loja_catalogo.por_id_venda('produto', extra.id)['preco'] == 18.50


def test_excecao_em_escopo_aninhado_restaura_escopo_externo(extra):
    with loja_leitura.catalogo_em_lote(base=BASE):
        externo = loja_leitura.atual()
        with pytest.raises(RuntimeError):
            with loja_leitura.catalogo_em_lote(base=BASE):
                assert loja_leitura.atual() is not externo
                raise RuntimeError('falha interna')
        assert loja_leitura.atual() is externo
    assert loja_leitura.atual() is None


def test_data_fora_do_periodo_precarregado_consulta_saldo_real(extra):
    db.session.add(EstoqueSitePlano(kind='produto', item_id=extra.id, data=_dia(40),
                                    qtd_planejada=0, qtd_reservada=0))
    db.session.commit()
    with loja_leitura.catalogo_em_lote(base=BASE):
        assert loja_catalogo._saldo_para_dia('produto', extra.id, _dia(40)) == 0


def test_calendario_lote_preserva_dia_fechado_janela_especial_e_limite_dois_dias(extra):
    db.session.add_all([
        LojaDataEspecial(data=_dia(3), rotulo='Fechado', janelas=''),
        LojaDataEspecial(data=_dia(4), rotulo='Especial', janelas='06:00–10:00'),
    ])
    db.session.commit()
    datas_antes = loja_checkout.datas_disponiveis('agendada', base=BASE, dias=31, lead_dias=2)
    with loja_leitura.catalogo_em_lote(base=BASE):
        datas = loja_checkout.datas_disponiveis('agendada', base=BASE, dias=31, lead_dias=2)
        assert datas == datas_antes
        assert _dia(3) not in datas
        assert datas[0] == _dia(2) and datas[-1] == _dia(32)
        assert loja_checkout.janelas_disponiveis(
            'agendada', _dia(4), base=BASE, distancia_km=20) == ['06:00–10:00']


@pytest.mark.parametrize('mudanca', ['preco', 'publicacao', 'saldo'])
def test_get_seguinte_busca_preco_publicacao_e_saldo_atualizados(app, kit, extra, mudanca):
    cliente, _, html = _abrir(app, kit)
    assert _ofertas(html)[('produto', extra.id)]['precoCentavos'] == 735
    assert loja_leitura.atual() is None
    if mudanca == 'preco':
        extra.preco_site = Decimal('8.25')
    elif mudanca == 'publicacao':
        extra.site_ativo = False
    else:
        db.session.add_all([EstoqueSitePlano(
            kind='produto', item_id=extra.id, data=_dia(dia),
            qtd_planejada=0, qtd_reservada=0) for dia in range(34)])
    db.session.commit()
    resposta = cliente.get(f'/loja/kits-cafe/{kit.id}')
    assert resposta.status_code == 200
    ofertas = _ofertas(resposta.get_data(as_text=True))
    assert loja_leitura.atual() is None
    if mudanca == 'preco':
        assert ofertas[('produto', extra.id)]['precoCentavos'] == 825
    else:
        assert ('produto', extra.id) not in ofertas


def test_post_cria_compra_fora_do_escopo_e_recusa_extra_despublicado(app, kit, extra, frete, monkeypatch):
    cliente, token, _ = _abrir(app, kit)
    original = compra_kits.criar_compra
    estados = []

    def verificar(*args, **kwargs):
        estados.append(loja_leitura.atual())
        return original(*args, **kwargs)

    monkeypatch.setattr(compra_kits, 'criar_compra', verificar)
    extra.site_ativo = False
    db.session.commit()
    resposta = _post(cliente, kit, token, adicionais_json=json.dumps([
        {'kind': 'produto', 'id': extra.id, 'qtd': 1}]))
    assert resposta.status_code == 400
    assert estados == [None]
    assert loja_leitura.atual() is None
    frete.assert_not_called()

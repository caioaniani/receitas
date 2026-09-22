"""Piso autorizado em 22/09/2026 para todas as lojas operacionais."""

import pytest

from app.extensions import db
from app.migrations_legacy import _seed_minimos_viennoiserie_todas_lojas
from app.models import AppConfig, EstoqueLoja, Loja, MovEstoqueLoja, Receita
from app.utils import agora

CHAVE = 'seed_minimos_viennoiserie_todas_lojas_2026_09_22'


def _receita(nome, categoria='Viennoiserie', **kwargs):
    receita = Receita(
        nome=nome, categoria=categoria, rendimento_qtd=1,
        rendimento_unidade='un', peso_base=100, **kwargs,
    )
    db.session.add(receita)
    return receita


def _cenario():
    receitas = [
        _receita('Choconana'),
        _receita('Danish de Maçã', categoria='Danishes'),
        _receita('Danish de Calabresa', categoria='Danishes'),
    ]
    lojas = [
        Loja(nome='Loja Ribeiro do Vale', ativa=True),
        Loja(nome='Loja Anesio Pinto Rosa', ativa=True),
        Loja(nome='Loja Nebraska', ativa=True, dias_funcionamento='0123456'),
        Loja(nome='BREAD & BREW', ativa=True, dias_funcionamento='01234'),
        Loja(nome='Cantina', ativa=True, dias_funcionamento='56'),
    ]
    db.session.add_all(lojas)
    db.session.commit()
    return receitas, lojas


def _estoque(loja, receita):
    return EstoqueLoja.query.filter_by(
        loja_id=loja.id, receita_id=receita.id,
    ).one()


def test_choconana_e_cada_danish_em_todas_lojas_inclusive_cantina(app):
    receitas, lojas = _cenario()

    _seed_minimos_viennoiserie_todas_lojas(app)

    assert EstoqueLoja.query.count() == len(receitas) * len(lojas)
    for loja in lojas:
        for receita in receitas:
            estoque = _estoque(loja, receita)
            assert estoque.pedido_minimo_diario == 2
            assert estoque.quantidade == 0
    assert AppConfig.get(CHAVE)
    assert MovEstoqueLoja.query.count() == 0


def test_seed_novo_aplica_apesar_dos_markers_anteriores(app):
    receitas, lojas = _cenario()
    for chave in (
        'seed_minimo_danish_2026_08',
        'seed_minimo_danish_2026_08_v2',
        'seed_minimo_danish_2026_08_v3',
        'seed_piso_dinamico_especiais_2026_09',
        'seed_regras_reposicao_lojas_2026_09',
    ):
        AppConfig.set(chave, 'concluido')
    db.session.add(EstoqueLoja(
        loja_id=lojas[0].id, receita_id=receitas[0].id,
        quantidade=8, pedido_minimo_diario=2,
    ))
    db.session.commit()

    _seed_minimos_viennoiserie_todas_lojas(app)

    assert _estoque(lojas[0], receitas[0]).quantidade == 8
    assert _estoque(lojas[2], receitas[0]).pedido_minimo_diario == 2
    assert _estoque(lojas[4], receitas[1]).pedido_minimo_diario == 2
    assert AppConfig.get(CHAVE)


def test_seed_preserva_pisos_maiores_saldo_reservas_e_outras_regras(app):
    receitas, lojas = _cenario()
    choconana, danish, _ = receitas
    choconana.estado_padrao = 'backup'
    choconana.preco_venda = 19.50
    choconana.lote_pedido = 8
    choconana.minimo_pedido = 16
    choconana.estoque_minimo_industria = 30
    maiores = EstoqueLoja(
        loja_id=lojas[2].id, receita_id=choconana.id,
        quantidade=17, quantidade_reservada=3, estoque_minimo=9,
        pedido_minimo_diario=5, reposicao_por_venda_diaria=True,
        estado='backup',
    )
    menor = EstoqueLoja(
        loja_id=lojas[2].id, receita_id=danish.id,
        quantidade=11, estoque_minimo=6, pedido_minimo_diario=1,
    )
    db.session.add_all([maiores, menor])
    db.session.commit()
    ids = maiores.id, menor.id

    _seed_minimos_viennoiserie_todas_lojas(app)

    assert _estoque(lojas[2], choconana).id == ids[0]
    assert _estoque(lojas[2], danish).id == ids[1]
    assert maiores.pedido_minimo_diario == 5
    assert menor.pedido_minimo_diario == 2
    assert (maiores.quantidade, maiores.quantidade_reservada) == (17, 3)
    assert (maiores.estoque_minimo, menor.estoque_minimo) == (9, 6)
    assert maiores.estado == 'backup'
    assert maiores.reposicao_por_venda_diaria is True
    assert menor.quantidade == 11
    assert choconana.estado_padrao == 'backup'
    assert choconana.preco_venda == 19.50
    assert (choconana.lote_pedido, choconana.minimo_pedido) == (8, 16)
    assert choconana.estoque_minimo_industria == 30
    assert MovEstoqueLoja.query.count() == 0


def test_seed_respeita_alteracao_manual_posterior_e_nao_duplica(app):
    receitas, lojas = _cenario()
    _seed_minimos_viennoiserie_todas_lojas(app)
    marker = AppConfig.get(CHAVE)
    removido = _estoque(lojas[1], receitas[0])
    reduzido = _estoque(lojas[4], receitas[1])
    removido.pedido_minimo_diario = None
    reduzido.pedido_minimo_diario = 1
    db.session.commit()

    _seed_minimos_viennoiserie_todas_lojas(app)

    db.session.refresh(removido)
    db.session.refresh(reduzido)
    assert removido.pedido_minimo_diario is None
    assert reduzido.pedido_minimo_diario == 1
    assert EstoqueLoja.query.count() == len(receitas) * len(lojas)
    assert AppConfig.get(CHAVE) == marker


def test_seed_exclui_industria_por_nome_normalizado_e_loja_inativa(app):
    receitas, _ = _cenario()
    excluidas = [
        Loja(nome='Indústria', ativa=True),
        Loja(nome='  INDÚSTRIA CENTRAL  ', ativa=True),
        Loja(nome='Loja encerrada', ativa=False),
    ]
    db.session.add_all(excluidas)
    db.session.commit()
    existente = EstoqueLoja(
        loja_id=excluidas[0].id, receita_id=receitas[0].id,
        quantidade=20, pedido_minimo_diario=7,
    )
    db.session.add(existente)
    db.session.commit()

    _seed_minimos_viennoiserie_todas_lojas(app)

    assert existente.pedido_minimo_diario == 7
    assert existente.quantidade == 20
    assert EstoqueLoja.query.filter_by(loja_id=excluidas[0].id).count() == 1
    for loja in excluidas[1:]:
        assert EstoqueLoja.query.filter_by(loja_id=loja.id).count() == 0


def test_seed_identifica_nome_categoria_e_exclui_receitas_nao_operacionais(app):
    loja = Loja(nome='Cantina', ativa=True, dias_funcionamento='56')
    db.session.add(loja)
    incluidas = [
        _receita('  CHOCONANA  '),
        _receita('  DÁNISH DE PISTACHE  ', categoria='Viennoiserie'),
        _receita('Folhado de pera', categoria='Danish'),
        _receita('Folhado de queijo', categoria='  DANISHES  '),
    ]
    excluidas = [
        _receita('Danish arquivado', arquivada_em=agora()),
        _receita('Danish recheio', sugerir_pedido_loja=False),
        _receita('Danish sob encomenda', sob_encomenda=True),
        _receita('Mini Danish de Maçã', categoria='Danishes'),
        _receita('Danish Mini de Chocolate', categoria='Danishes'),
        _receita('Choconana especial'),
        _receita('Choconana', sob_encomenda=True),
        _receita('Choconana', sugerir_pedido_loja=False),
        _receita('Choconana', arquivada_em=agora()),
        _receita('Creme de Danish', categoria='Sub-receitas'),
        _receita('Cinnamon Roll'),
    ]
    db.session.commit()

    _seed_minimos_viennoiserie_todas_lojas(app)

    pisos = {e.receita_id: e.pedido_minimo_diario for e in EstoqueLoja.query}
    assert pisos == {receita.id: 2 for receita in incluidas}
    assert all(receita.id not in pisos for receita in excluidas)


@pytest.mark.parametrize('faltante', ['choconana', 'danish', 'loja'])
def test_cadastro_incompleto_nao_grava_marker_e_retenta_apos_cadastro(app, faltante):
    choconana = None if faltante == 'choconana' else _receita('Choconana')
    danish = None if faltante == 'danish' else _receita('Danish de Maçã')
    loja = None if faltante == 'loja' else Loja(nome='Cantina', ativa=True)
    if loja:
        db.session.add(loja)
    db.session.commit()

    _seed_minimos_viennoiserie_todas_lojas(app)

    assert not AppConfig.get(CHAVE)
    assert EstoqueLoja.query.count() == 0
    if choconana is None:
        choconana = _receita('Choconana')
    if danish is None:
        danish = _receita('Danish de Maçã')
    if loja is None:
        loja = Loja(nome='Cantina', ativa=True)
        db.session.add(loja)
    db.session.commit()

    _seed_minimos_viennoiserie_todas_lojas(app)

    assert _estoque(loja, choconana).pedido_minimo_diario == 2
    assert _estoque(loja, danish).pedido_minimo_diario == 2
    assert AppConfig.get(CHAVE)

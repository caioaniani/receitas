"""A reserva não pode fabricar MP ao virar consumo ou esconder um déficit.

O saldo disponível inclui a reserva pendente. Quantidade assinada preserva
essa identidade, inclusive quando a entrada física ainda não foi registrada.
"""

import pytest

from app.extensions import db
from app.models import (
    EstoqueProducao,
    MateriaPrima,
    MovimentacaoEstoque,
    PlanejamentoItem,
    PlanejamentoProducao,
    PreBaixaMP,
    Receita,
    ReceitaIngrediente,
)
from app.services.bateladas_paes import normalizar_item
from app.services.producao import (
    excluir_plano_do_dia,
    produzir_item_plano,
    sincronizar_pre_baixa_mp,
)
from app.utils import hoje


def _ordem(estoque_farinha, *, bateladas=1, sabores=1,
           origem='cronograma', reservar=True, com_levain=False):
    farinha = MateriaPrima(nome='Farinha de trigo', custo_por_kg=5,
                           estoque_atual=estoque_farinha)
    agua = MateriaPrima(nome='Água', custo_por_kg=1, estoque_atual=1000000)
    plano = PlanejamentoProducao(data=hoje(), origem=origem,
                                enviado_ao_padeiro=True)
    db.session.add_all([farinha, agua, plano])
    db.session.flush()
    if com_levain:
        levain = Receita(nome='Levain da produção', categoria='Insumos',
                         peso_base=1000, peso_unitario=1, rendimento_qtd=1,
                         rendimento_unidade='g', sub_na_amassadeira=True)
        db.session.add(levain)
        db.session.flush()
        db.session.add(EstoqueProducao(receita_id=levain.id, quantidade=100000))
    itens = []
    for idx in range(sabores):
        receita = Receita(nome=f'Sourdough sabor {idx}', categoria='Pães',
                          peso_base=1000, peso_unitario=600,
                          rendimento_qtd=10, rendimento_unidade='un',
                          capacidade_amassadeira_g=50000)
        db.session.add(receita)
        db.session.flush()
        db.session.add_all([
            ReceitaIngrediente(receita_id=receita.id, tipo='mp',
                               ingrediente_nome='Farinha de trigo', porcentagem=100),
            ReceitaIngrediente(receita_id=receita.id, tipo='mp',
                               ingrediente_nome='Água', porcentagem=70),
        ])
        if com_levain:
            db.session.add(ReceitaIngrediente(
                receita_id=receita.id, tipo='sub_pct', ingrediente_nome=levain.nome,
                porcentagem=10, sub_receita_id=levain.id))
        item = PlanejamentoItem(planejamento_id=plano.id, receita=receita,
                                qtd_alvo=70 * bateladas, produzido_qtd=0,
                                multiplicador=1)
        db.session.add(item)
        normalizar_item(item)
        itens.append(item)
    if reservar:
        sincronizar_pre_baixa_mp(plano, criar=True)
    db.session.commit()
    return plano, itens, farinha


@pytest.mark.parametrize('estoque_inicial', [10000, 30000, 100000])
def test_confirmacao_nao_fabrica_farinha_quando_reserva_vira_consumo(app, admin_user, estoque_inicial):
    plano, itens, farinha = _ordem(estoque_inicial)
    assert farinha.estoque_atual == pytest.approx(estoque_inicial - 25000)
    resultado = produzir_item_plano(itens[0].id, 70, admin_user.id)
    assert resultado['ok']
    assert farinha.estoque_atual == pytest.approx(estoque_inicial - 25000)
    assert PreBaixaMP.query.filter_by(plano_id=plano.id,
                                      materia_prima_id=farinha.id).one().quantidade == 0


@pytest.mark.parametrize('estoque_inicial', [10000, 30000])
def test_confirmacoes_parciais_preservam_saldo_disponivel_negativo(app, admin_user, estoque_inicial):
    plano, itens, farinha = _ordem(estoque_inicial, bateladas=2)
    esperado = estoque_inicial - 50000
    assert farinha.estoque_atual == pytest.approx(esperado)
    for parcial, reserva in [(35, 37500), (35, 25000), (70, 0)]:
        assert produzir_item_plano(itens[0].id, parcial, admin_user.id)['ok']
        assert farinha.estoque_atual == pytest.approx(esperado)
        assert PreBaixaMP.query.filter_by(
            plano_id=plano.id, materia_prima_id=farinha.id).one().quantidade == pytest.approx(reserva)


def test_consumo_de_um_sabor_preserva_reserva_do_outro(app, admin_user):
    plano, itens, farinha = _ordem(30000, sabores=2)
    assert farinha.estoque_atual == -20000
    assert produzir_item_plano(itens[0].id, 70, admin_user.id)['ok']
    assert farinha.estoque_atual == -20000
    assert PreBaixaMP.query.filter_by(
        plano_id=plano.id, materia_prima_id=farinha.id).one().quantidade == 25000
    assert produzir_item_plano(itens[1].id, 70, admin_user.id)['ok']
    assert farinha.estoque_atual == -20000


def test_excluir_ordem_sem_producao_devolve_so_o_saldo_original(app):
    _plano, _itens, farinha = _ordem(10000)
    assert farinha.estoque_atual == -15000
    assert excluir_plano_do_dia(hoje())['ok']
    assert farinha.estoque_atual == 10000
    assert PreBaixaMP.query.count() == 0


def test_reduzir_ordem_estorna_apenas_uma_batelada(app):
    plano, itens, farinha = _ordem(30000, bateladas=2)
    assert farinha.estoque_atual == -20000
    itens[0].qtd_alvo = 70
    normalizar_item(itens[0])
    sincronizar_pre_baixa_mp(plano)
    db.session.commit()
    assert farinha.estoque_atual == 5000
    assert PreBaixaMP.query.filter_by(
        plano_id=plano.id, materia_prima_id=farinha.id).one().quantidade == 25000


def _cliente(app, usuario):
    cliente = app.test_client()
    with cliente.session_transaction() as sess:
        sess['_user_id'] = str(usuario.id)
        sess['_fresh'] = True
    return cliente


def test_baixa_manual_usa_executor_com_subreceitas_e_nao_repete(app, admin_user):
    plano, itens, farinha = _ordem(
        100000, sabores=2, origem='manual', reservar=False, com_levain=True)
    cliente = _cliente(app, admin_user)
    for _ in range(2):
        resposta = cliente.post(f'/producao/{plano.id}/baixar-estoque')
        assert resposta.status_code == 302
        db.session.expire_all()
        assert plano.status == 'executado'
        assert farinha.estoque_atual == 50000
        for item in itens:
            assert item.produzido_qtd == item.qtd_alvo == 75
            assert EstoqueProducao.query.filter_by(
                receita_id=item.receita_id).one().quantidade == 75
        levain = Receita.query.filter_by(nome='Levain da produção').one()
        assert EstoqueProducao.query.filter_by(receita_id=levain.id).one().quantidade == 95000
    saidas_farinha = MovimentacaoEstoque.query.filter_by(materia_prima_id=farinha.id).all()
    assert len(saidas_farinha) == 2
    assert sum(m.quantidade for m in saidas_farinha) == 50000


def test_baixa_manual_toda_transacao_reverte_se_segundo_sabor_falhar(app, admin_user, monkeypatch):
    from app.services import producao
    plano, itens, farinha = _ordem(100000, sabores=2, origem='manual', reservar=False)
    original = producao.produzir_item_plano
    chamadas = []

    def falhar_segundo(item_id, unidades, user_id, **kwargs):
        chamadas.append(item_id)
        if len(chamadas) == 2:
            raise ValueError('Falha controlada no segundo sabor.')
        assert kwargs.get('commit') is False
        return original(item_id, unidades, user_id, **kwargs)

    monkeypatch.setattr(producao, 'produzir_item_plano', falhar_segundo)
    resposta = _cliente(app, admin_user).post(f'/producao/{plano.id}/baixar-estoque')
    assert resposta.status_code == 302
    assert len(chamadas) == 2
    db.session.expire_all()
    assert plano.status != 'executado'
    assert all(item.produzido_qtd == 0 for item in itens)
    assert farinha.estoque_atual == 100000
    assert EstoqueProducao.query.count() == 0
    assert MovimentacaoEstoque.query.count() == 0


def test_baixa_generica_de_cronograma_nao_duplica_reserva(app, admin_user):
    plano, itens, farinha = _ordem(30000)
    antes = MovimentacaoEstoque.query.count()
    resposta = _cliente(app, admin_user).post(f'/producao/{plano.id}/baixar-estoque')
    assert resposta.status_code == 302
    assert '/padeiro/' in resposta.location
    db.session.expire_all()
    assert farinha.estoque_atual == 5000
    assert itens[0].produzido_qtd == 0
    assert EstoqueProducao.query.count() == 0
    assert MovimentacaoEstoque.query.count() == antes


def test_exclusao_generica_estorna_reserva_sem_fabricar_saldo(app, admin_user):
    plano, _itens, farinha = _ordem(10000)
    plano_id = plano.id
    resposta = _cliente(app, admin_user).post(f'/producao/{plano_id}/excluir')
    assert resposta.status_code == 302
    db.session.expire_all()
    assert farinha.estoque_atual == 10000
    assert db.session.get(PlanejamentoProducao, plano_id) is None
    assert PreBaixaMP.query.count() == 0


@pytest.mark.parametrize('origem', ['cronograma', 'manual'])
def test_exclusao_generica_recusa_ordem_com_producao_preserva_snapshot(app, admin_user, origem):
    plano, itens, farinha = _ordem(30000, origem=origem, reservar=origem == 'cronograma')
    produzir_item_plano(itens[0].id, 35, admin_user.id)
    saldo = farinha.estoque_atual
    resposta = _cliente(app, admin_user).post(f'/producao/{plano.id}/excluir')
    assert resposta.status_code == 302
    db.session.expire_all()
    assert db.session.get(PlanejamentoProducao, plano.id) is not None
    assert itens[0].produzido_qtd == 35
    assert itens[0].batelada_padrao is not None
    assert farinha.estoque_atual == saldo
    assert EstoqueProducao.query.filter_by(receita_id=itens[0].receita_id).one().quantidade == 35

"""Regressões do caminho real: aprovar, enviar, editar, produzir e comprar."""

from copy import deepcopy
from datetime import date, timedelta

import pytest

from app.extensions import db
from app.models import (
    CronogramaOverride,
    EstoqueProducao,
    MateriaPrima,
    MovimentacaoEstoque,
    PlanejamentoItem,
    PlanejamentoProducao,
    PreBaixaMP,
    Receita,
    ReceitaIngrediente,
)
from app.services.bateladas_paes import resumo_item
from app.services.producao import (
    aprovar_plano_do_dia,
    consolidar_lista_compras,
    enviar_plano_do_dia,
    produzir_item_plano,
)

DIA = date(2026, 9, 21)


def _cenario(nome='Sourdough Tradicional', qtd=17):
    rec = Receita(nome=nome, categoria='Pães', peso_base=1000,
                  rendimento_qtd=4, rendimento_unidade='un', peso_unitario=500,
                  capacidade_amassadeira_g=112000)
    levain = Receita(nome='Levain (pé)', categoria='Insumos', peso_base=1000,
                     rendimento_qtd=1, rendimento_unidade='g', peso_unitario=1,
                     sub_na_amassadeira=True)
    db.session.add_all([rec, levain])
    db.session.flush()
    for nome_mp, pct in [('Farinha', 100), ('Água', 80), ('Sal', 2), ('Fermento', .1)]:
        db.session.add(MateriaPrima(nome=nome_mp, unidade='g', custo_por_kg=4,
                                    estoque_atual=100000))
        rec.ingredientes.append(ReceitaIngrediente(
            tipo='mp', ingrediente_nome=nome_mp, porcentagem=pct))
    rec.ingredientes.append(ReceitaIngrediente(
        tipo='receita', ingrediente_nome=levain.nome, porcentagem=200,
        sub_receita=levain))
    db.session.add(EstoqueProducao(receita_id=levain.id, quantidade=50000))
    db.session.commit()
    return rec, _crono(rec, qtd)


def _crono(rec, qtd):
    return {'receitas': [{'receita_id': rec.id, 'por_dia': [
        {'data': DIA.isoformat(), 'qtd': qtd}]}]}


def _farinha():
    return MateriaPrima.query.filter_by(nome='Farinha').one()


def _reserva(plano, mp):
    return PreBaixaMP.query.filter_by(plano_id=plano.id, materia_prima_id=mp.id).one()


def _client(app, user):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    return c


def test_reenvio_sem_demanda_nao_reabre_batelada_parcial(app, admin_user):
    rec, crono = _cenario()
    plano = enviar_plano_do_dia(DIA, admin_user.id, crono=crono)
    item = plano.itens[0]
    assert produzir_item_plano(item.id, 50, admin_user.id, encerrar=True)['ok']
    enviar_plano_do_dia(DIA, admin_user.id, crono=_crono(rec, 0))
    assert item.qtd_alvo == item.produzido_qtd == 50
    assert _reserva(plano, _farinha()).quantidade == 0


def test_insumo_congelado_nao_reabre_apos_editar_ficha(app, admin_user, monkeypatch):
    rec, crono = _cenario()
    levain = Receita.query.filter_by(nome='Levain (pé)').one()
    levain.dias_producao = 1
    db.session.commit()
    plano = enviar_plano_do_dia(DIA, admin_user.id, crono=crono)
    item = plano.itens[0]
    alvo = item.qtd_alvo
    assert item.batelada_padrao.dados['antecedencia_insumo_dias'] > 0
    rec.ingredientes[:] = [i for i in rec.ingredientes if i.tipo != 'receita']
    db.session.commit()
    monkeypatch.setattr('app.services.producao.hoje', lambda: DIA - timedelta(days=1))
    monkeypatch.setenv('FREEZE_INSUMO', '1')
    enviar_plano_do_dia(DIA, user_id=None, crono=_crono(rec, 900))
    assert item.qtd_alvo == alvo


@pytest.mark.parametrize(('nome', 'farinha', 'alvo'), [
    ('Sourdough Tradicional', 25000, 101), ('Pão Francês Fermentado', 12000, 48),
])
def test_enviar_normaliza_e_reserva_batelada_inteira(app, admin_user, nome, farinha, alvo):
    rec, crono = _cenario(nome)
    plano = enviar_plano_do_dia(DIA, admin_user.id, crono=crono)
    it = plano.itens[0]
    assert it.receita_id == rec.id
    assert it.qtd_alvo == alvo
    assert it.batelada_padrao.bateladas == 1
    assert plano.enviado_ao_padeiro
    assert _reserva(plano, _farinha()).quantidade == farinha
    assert _farinha().estoque_atual == 100000 - farinha
    assert EstoqueProducao.query.filter_by(receita_id=rec.id).first() is None


def test_produzir_duas_partes_credita_lote_e_nao_debita_farinha_duas_vezes(app, admin_user):
    rec, crono = _cenario()
    plano = enviar_plano_do_dia(DIA, admin_user.id, crono=crono)
    it = plano.itens[0]
    assert produzir_item_plano(it.id, 40, admin_user.id)['ok']
    assert it.produzido_qtd == 40
    assert _reserva(plano, _farinha()).quantidade == pytest.approx(25000 * 61 / 101)
    assert _farinha().estoque_atual == pytest.approx(75000)
    assert produzir_item_plano(it.id, 61, admin_user.id)['ok']
    assert it.produzido_qtd == 101
    assert it.qtd_alvo == 101
    assert EstoqueProducao.query.filter_by(receita_id=rec.id).one().quantidade == 101
    assert _reserva(plano, _farinha()).quantidade == pytest.approx(0)
    assert _farinha().estoque_atual == pytest.approx(75000)
    reais = MovimentacaoEstoque.query.filter(
        MovimentacaoEstoque.materia_prima_id == _farinha().id,
        MovimentacaoEstoque.referencia.like('Produção Sourdough%'),
    ).all()
    assert len(reais) == 2
    assert sum(m.quantidade for m in reais) == pytest.approx(25000)


def test_reserva_quase_esgota_mp_confirmacao_nao_cria_saldo_ficticio(app, admin_user):
    rec, crono = _cenario()
    _farinha().estoque_atual = 30000
    db.session.commit()
    plano = enviar_plano_do_dia(DIA, admin_user.id, crono=crono)
    assert _farinha().estoque_atual == 5000
    for quantidade in (40, 61):
        produzir_item_plano(plano.itens[0].id, quantidade, admin_user.id)
        assert _farinha().estoque_atual == pytest.approx(5000)
    assert EstoqueProducao.query.filter_by(receita_id=rec.id).one().quantidade == 101


def test_reenviar_e_editar_ficha_nao_muda_snapshot_nem_reserva(app, admin_user):
    rec, crono = _cenario()
    plano = enviar_plano_do_dia(DIA, admin_user.id, crono=crono)
    original = deepcopy(plano.itens[0].batelada_padrao.dados)
    qtd_movs = MovimentacaoEstoque.query.count()
    rec.ingredientes[0].porcentagem = 10
    rec.peso_unitario = 333
    rec.nome = 'Sourdough renomeado'
    db.session.commit()
    reenviado = enviar_plano_do_dia(DIA, admin_user.id, crono=crono)
    assert reenviado.id == plano.id
    assert reenviado.itens[0].qtd_alvo == 101
    assert reenviado.itens[0].batelada_padrao.dados == original
    assert MovimentacaoEstoque.query.count() == qtd_movs
    assert _farinha().estoque_atual == 75000
    produzir_item_plano(plano.itens[0].id, 101, admin_user.id)
    assert _farinha().estoque_atual == pytest.approx(75000)


def test_compras_usa_pesagem_congelada_e_nao_multiplicador_arredondado(app, admin_user):
    rec, crono = _cenario()
    plano = enviar_plano_do_dia(DIA, admin_user.id, crono=crono)
    it = plano.itens[0]
    rec.ingredientes[0].porcentagem = 9
    it.multiplicador = 999
    db.session.commit()
    compras = consolidar_lista_compras([
        {'receita_id': rec.id, 'multiplicador': it.multiplicador, 'item_plano': it}])
    assert compras['Farinha']['quantidade'] == 25000
    assert compras['Farinha']['custo_estimado'] == 100
    assert compras['Água']['quantidade'] == 20000


def test_legado_enviado_sem_snapshot_nao_adota_batelada_no_reenvio(app, admin_user):
    rec, crono = _cenario()
    plano = PlanejamentoProducao(data=DIA, origem='cronograma',
                                 enviado_ao_padeiro=True, status='aprovado')
    it = PlanejamentoItem(planejamento=plano, receita=rec, qtd_alvo=17,
                          produzido_qtd=0, multiplicador=5)
    db.session.add_all([plano, it])
    db.session.commit()
    enviar_plano_do_dia(DIA, admin_user.id, crono=crono)
    assert it.qtd_alvo == 17
    assert it.batelada_padrao is None
    assert _reserva(plano, _farinha()).quantidade == pytest.approx(17000 / 4.042)


def test_aprovar_rascunho_normaliza_sem_debitar_estoque(app, admin_user):
    rec, crono = _cenario()
    plano = aprovar_plano_do_dia(DIA, admin_user.id, crono=crono)
    assert plano.enviado_ao_padeiro is False
    assert plano.itens[0].qtd_alvo == 101
    assert plano.itens[0].batelada_padrao is not None
    assert _farinha().estoque_atual == 100000
    assert PreBaixaMP.query.count() == 0


def test_editar_plano_arredonda_e_espelha_override_sem_producao_ficticia(app, admin_user):
    rec, crono = _cenario()
    plano = aprovar_plano_do_dia(DIA, admin_user.id, crono=crono)
    it = plano.itens[0]
    resp = _client(app, admin_user).post('/padeiro/plano/editar', data={
        'data': DIA.isoformat(), f'alvo_{it.id}': '102'})
    assert resp.status_code == 302
    db.session.expire_all()
    assert it.qtd_alvo == 202
    assert resumo_item(it)['bateladas'] == 2
    assert CronogramaOverride.query.filter_by(receita_id=rec.id, data=DIA).one().qtd == 202
    assert it.produzido_qtd == 0
    assert _farinha().estoque_atual == 100000


def test_editar_plano_enviado_reajusta_so_delta_da_reserva(app, admin_user):
    rec, crono = _cenario()
    plano = enviar_plano_do_dia(DIA, admin_user.id, crono=crono)
    it = plano.itens[0]
    resp = _client(app, admin_user).post('/padeiro/plano/editar', data={
        'data': DIA.isoformat(), f'alvo_{it.id}': '150'})
    assert resp.status_code == 302
    db.session.expire_all()
    assert it.qtd_alvo == 202
    assert _reserva(plano, _farinha()).quantidade == 50000
    assert _farinha().estoque_atual == 50000


def test_novo_manual_multiplicador_representa_numero_bateladas(app, admin_user):
    rec, _ = _cenario()
    resp = _client(app, admin_user).post('/producao/novo', data={
        'data': DIA.isoformat(), 'nome': 'Três bateladas independentes',
        'receita_id[]': str(rec.id), 'multiplicador[]': '3'})
    assert resp.status_code == 302
    plano = PlanejamentoProducao.query.one()
    it = plano.itens[0]
    assert it.qtd_alvo == 303
    assert it.batelada_padrao.bateladas == 3
    assert resumo_item(it)['farinha_total_g'] == 75000
    assert _farinha().estoque_atual == 100000


def test_novo_manual_ficha_invalida_nao_grava_ordem_parcial(app, admin_user):
    rec, _ = _cenario()
    rec.ingredientes[0].ingrediente_nome = 'Farinha sem cadastro'
    db.session.commit()
    resp = _client(app, admin_user).post('/producao/novo', data={
        'data': DIA.isoformat(), 'receita_id[]': str(rec.id), 'multiplicador[]': '1'})
    assert resp.status_code == 302
    assert resp.location.endswith('/producao/novo')
    assert PlanejamentoProducao.query.count() == 0
    assert MovimentacaoEstoque.query.count() == 0

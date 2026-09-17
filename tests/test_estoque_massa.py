"""A massa de 25 kg de farinha não perde o resto de uma bola histórica."""

from decimal import Decimal

import pytest

from app.extensions import db
from app.models import (
    ConsumoSubFracao,
    EstoqueProducao,
    MovEstoqueMassa,
    MovEstoqueProducao,
    Receita,
    SaldoResidualMassa,
)
from app.services.estoque_massa import (
    ajustar_contagem_bolas,
    consumir_massa,
    eh_massa_folhar,
    estornar_movimentos_massa,
    peso_bola_g,
    registrar_entrada_bolas,
    registrar_entrada_massa,
    saldo_bolas,
)


def _massa(*, bolas=0, fracao=None):
    rec = Receita(nome='Massa para folhar', categoria='Insumos', peso_base=2000,
                  rendimento_qtd=1, rendimento_unidade='un', peso_unitario=3580)
    db.session.add(rec)
    db.session.flush()
    ep = EstoqueProducao(receita_id=rec.id, quantidade=bolas)
    db.session.add(ep)
    if fracao is not None:
        db.session.add(ConsumoSubFracao(receita_id=rec.id, fracao_pendente=fracao))
    db.session.commit()
    return rec, ep


def _total_g(rec, ep):
    residual = db.session.get(SaldoResidualMassa, rec.id)
    assert Decimal(0) <= residual.g < residual.peso_bola_g
    return Decimal(ep.quantidade) * residual.peso_bola_g + residual.g


def test_entrada_de_batimento_conserva_44900_gramas(app):
    rec, ep = _massa()
    out = registrar_entrada_massa(rec, 44900, None, 'Batimento 25 kg de farinha')
    db.session.commit()
    assert ep.quantidade == 12
    assert db.session.get(SaldoResidualMassa, rec.id).g == Decimal('1940.000000')
    assert _total_g(rec, ep) == Decimal(44900)
    assert out['bolas'] == Decimal(44900) / Decimal(3580)
    assert saldo_bolas(rec) == Decimal(44900) / Decimal(3580)
    mov = MovEstoqueMassa.query.one()
    inteiro = db.session.get(MovEstoqueProducao, mov.movimento_inteiro_id)
    assert mov.quantidade_g == Decimal(44900)
    assert (mov.saldo_anterior_g, mov.saldo_posterior_g) == (0, 44900)
    assert inteiro.tipo == 'producao' and inteiro.quantidade == 12


def test_consumo_fracionario_debita_massa_imediatamente(app):
    rec, ep = _massa()
    registrar_entrada_massa(rec, 44900, None, 'Batimento')
    out = consumir_massa(rec, Decimal('0.6'), None, 'Croissants')
    db.session.commit()
    assert out == {'baixado': Decimal('0.6'), 'falta': 0,
                   'baixado_g': Decimal(2148), 'falta_g': 0}
    assert ep.quantidade == 11
    assert db.session.get(SaldoResidualMassa, rec.id).g == Decimal(3372)
    assert _total_g(rec, ep) == Decimal(42752)
    assert ConsumoSubFracao.query.count() == 0
    debito = MovEstoqueMassa.query.filter_by(tipo='consumo_subreceita').one()
    assert debito.quantidade_g == Decimal(2148)
    assert db.session.get(MovEstoqueProducao, debito.movimento_inteiro_id).quantidade == 1


def test_dois_batimentos_acumulam_resto_sem_perder_bola(app):
    rec, ep = _massa()
    # Mesmo sem commit entre as entradas: o lock/reload vê o saldo da transação.
    registrar_entrada_massa(rec, 44900, None, 'Batimento 1')
    registrar_entrada_massa(rec, 44900, None, 'Batimento 2')
    db.session.commit()
    assert ep.quantidade == 25
    assert db.session.get(SaldoResidualMassa, rec.id).g == Decimal(300)
    assert _total_g(rec, ep) == Decimal(89800)
    assert [m.quantidade for m in MovEstoqueProducao.query.order_by(
        MovEstoqueProducao.id).all()] == [12, 13]
    assert sum(m.quantidade_g for m in MovEstoqueMassa.query.all()) == Decimal(89800)


def test_leitura_nao_migra_fracao_nem_cria_saldo(app):
    rec, ep = _massa(bolas=2, fracao=0.25)
    assert saldo_bolas(rec) == Decimal('1.75')
    assert ep.quantidade == 2
    assert SaldoResidualMassa.query.count() == 0
    assert MovEstoqueMassa.query.count() == 0
    assert ConsumoSubFracao.query.one().fracao_pendente == 0.25


def test_leitura_legada_sem_peso_dispensa_conversao_mas_escrita_recusa(app):
    rec, ep = _massa(bolas=2, fracao=0.25)
    rec.peso_unitario = None
    db.session.commit()
    assert saldo_bolas(rec) == Decimal('1.75')
    assert ep.quantidade == 2
    assert SaldoResidualMassa.query.count() == MovEstoqueMassa.query.count() == 0
    with pytest.raises(ValueError, match='Peso da bola'):
        registrar_entrada_massa(rec, 44900, None, 'Sem unidade válida')
    db.session.rollback()
    assert SaldoResidualMassa.query.count() == MovEstoqueMassa.query.count() == 0


def test_primeira_escrita_consolida_fracao_anterior_uma_vez(app):
    rec, ep = _massa(bolas=2, fracao=0.25)
    old = MovEstoqueProducao(estoque_producao_id=ep.id, tipo='producao',
                             quantidade=2, referencia='Histórico anterior')
    db.session.add(old)
    db.session.commit()
    consumir_massa(rec, Decimal('0.5'), None, 'Danish')
    assert _total_g(rec, ep) == Decimal(4475)  # (2 - .25 - .5) × 3580
    assert ConsumoSubFracao.query.one().fracao_pendente == 0
    consumir_massa(rec, Decimal('0.25'), None, 'Mais Danish')
    db.session.commit()
    assert saldo_bolas(rec) == 1
    assert _total_g(rec, ep) == Decimal(3580)
    assert (old.quantidade, old.referencia) == (2, 'Histórico anterior')
    debitos = MovEstoqueMassa.query.filter_by(tipo='consumo_subreceita').all()
    assert sum(m.quantidade_g for m in debitos) == Decimal(3580)
    assert len([m for m in debitos if m.referencia ==
                'Conversão da fração de consumo anterior']) == 1


def test_consumo_registra_falta_inferior_a_uma_bola(app):
    rec, ep = _massa()
    registrar_entrada_massa(rec, 1790, None, 'Meia bola medida')
    out = consumir_massa(rec, Decimal('0.75'), None, 'Montagem')
    db.session.commit()
    assert out == {'baixado': Decimal('0.5'), 'falta': Decimal('0.25'),
                   'baixado_g': Decimal(1790), 'falta_g': Decimal(895)}
    assert ep.quantidade == 0
    assert _total_g(rec, ep) == 0
    falta = MovEstoqueMassa.query.filter_by(tipo='consumo_subreceita_sem_estoque').one()
    assert falta.quantidade_g == Decimal(895)
    assert falta.saldo_anterior_g == falta.saldo_posterior_g == 0
    assert falta.movimento_inteiro_id is None
    assert MovEstoqueProducao.query.count() == 0


def test_falta_legada_nao_consume_entrada_futura(app):
    rec, ep = _massa(fracao=0.25)
    assert saldo_bolas(rec) == Decimal('-0.25')
    registrar_entrada_massa(rec, 44900, None, 'Produção nova')
    db.session.commit()
    assert _total_g(rec, ep) == Decimal(44900)
    assert ConsumoSubFracao.query.one().fracao_pendente == 0
    falta = MovEstoqueMassa.query.filter_by(tipo='consumo_subreceita_sem_estoque').one()
    assert falta.quantidade_g == Decimal(895)


def test_rollback_reverte_entrada_consumo_e_conversao_legada(app):
    rec, ep = _massa(bolas=2, fracao=0.25)
    registrar_entrada_massa(rec, 44900, None, 'Entrada a reverter')
    consumir_massa(rec, 1, None, 'Consumo a reverter')
    db.session.flush()
    assert MovEstoqueMassa.query.count() == 3
    db.session.rollback()
    assert ep.quantidade == 2
    assert ConsumoSubFracao.query.one().fracao_pendente == 0.25
    assert SaldoResidualMassa.query.count() == 0
    assert MovEstoqueMassa.query.count() == 0
    assert MovEstoqueProducao.query.count() == 0


def test_contagem_absoluta_zera_resto_mesmo_com_inteiro_igual(app):
    rec, ep = _massa()
    registrar_entrada_massa(rec, 44900, None, 'Batimento')
    out = ajustar_contagem_bolas(rec, 12, None, 'Conferência física')
    db.session.commit()
    assert out['delta_g'] == Decimal(-1940)
    assert ep.quantidade == 12
    assert _total_g(rec, ep) == Decimal(42960)
    ajuste = MovEstoqueMassa.query.filter_by(tipo='ajuste_conferencia').one()
    assert ajuste.quantidade_g == Decimal(1940)
    assert ajuste.movimento_inteiro_id is None
    # A mesma contagem é idempotente, sem movimento vazio.
    ajustar_contagem_bolas(rec, 12, None, 'Conferência repetida')
    db.session.commit()
    assert MovEstoqueMassa.query.filter_by(tipo='ajuste_conferencia').count() == 1


def test_contagem_fisica_elimina_fracao_legada_e_audita_delta_assinado(app):
    rec, ep = _massa(bolas=3, fracao=0.25)
    ajustar_contagem_bolas(rec, 1, None, 'Contagem de uma bola')
    db.session.commit()
    assert ep.quantidade == 1 and saldo_bolas(rec) == 1
    assert ConsumoSubFracao.query.one().fracao_pendente == 0
    mov = MovEstoqueProducao.query.filter_by(tipo='ajuste_conferencia').one()
    assert mov.quantidade == -1  # conversão anterior já levou o inteiro 3 → 2


def test_peso_historico_nao_muda_com_edicao_da_ficha(app):
    rec, ep = _massa()
    registrar_entrada_massa(rec, 44900, None, 'Batimento inicial')
    db.session.commit()
    rec.peso_unitario = 4000
    db.session.commit()
    assert saldo_bolas(rec) == Decimal(44900) / Decimal(3580)
    consumir_massa(rec, 1, None, 'Uma bola histórica')
    db.session.commit()
    assert _total_g(rec, ep) == Decimal(41320)
    assert db.session.get(SaldoResidualMassa, rec.id).peso_bola_g == Decimal(3580)


@pytest.mark.parametrize('entrada', ['NaN', 'Infinity', '-1', '0', '1e100'])
def test_entrada_invalida_nao_muda_estoque(app, entrada):
    rec, ep = _massa(bolas=1)
    with pytest.raises(ValueError):
        registrar_entrada_massa(rec, entrada, None, 'Inválida')
    assert ep.quantidade == 1
    assert SaldoResidualMassa.query.count() == MovEstoqueMassa.query.count() == 0


@pytest.mark.parametrize('nome, esperado', [
    ('  MASSA  PARA FOLHAR ', True),
    ('Massa de Danish', False),
    ('Croissant com Massa para folhar', False),
    ('Massa para folhar - Retorno', False),
])
def test_identidade_e_exata(app, nome, esperado):
    assert eh_massa_folhar(Receita(nome=nome)) is esperado


def test_bolas_avulsas_preservam_resto_e_podem_esvaziar_toda_massa(app):
    rec, ep = _massa()
    registrar_entrada_massa(rec, 44900, None, 'Batimento')
    registrar_entrada_massa(rec, 3580, None, 'Bola avulsa')
    consumir_massa(rec, 1, None, 'Saída avulsa')
    assert _total_g(rec, ep) == Decimal(44900)
    consumir_massa(rec, Decimal(44900) / Decimal(3580), None, 'Montagem de tudo')
    db.session.commit()
    assert ep.quantidade == 0 and _total_g(rec, ep) == 0


def test_primeira_entrada_usa_peso_congelado_antes_de_edicao_da_ficha(app):
    rec, ep = _massa()
    rec.peso_unitario = 4000
    db.session.commit()
    registrar_entrada_massa(rec, 44900, None, 'Ordem congelada',
                            peso_bola_g_snapshot=3580)
    db.session.commit()
    assert ep.quantidade == 12
    assert peso_bola_g(rec) == Decimal(3580)
    assert db.session.get(SaldoResidualMassa, rec.id).g == Decimal(1940)


def test_entrada_avulsa_converte_bola_no_peso_do_saldo_congelado(app):
    rec, ep = _massa()
    registrar_entrada_massa(rec, 44900, None, 'Ordem congelada')
    db.session.commit()
    rec.peso_unitario = 4000
    db.session.commit()
    registrar_entrada_bolas(rec, 1, None, 'Entrada avulsa')
    db.session.commit()
    assert ep.quantidade == 13
    assert _total_g(rec, ep) == Decimal(48480)


def test_massa_renomeada_com_saldo_mantem_entrada_e_consumo(app):
    rec, ep = _massa()
    registrar_entrada_massa(rec, 44900, None, 'Antes de renomear')
    rec.nome = 'Massa laminada da casa'
    db.session.commit()
    assert eh_massa_folhar(rec)
    registrar_entrada_bolas(rec, 1, None, 'Entrada depois de renomear')
    consumir_massa(rec, 1, None, 'Consumo depois de renomear')
    db.session.commit()
    assert _total_g(rec, ep) == Decimal(44900)


def test_snapshot_mantem_identidade_renomeada_antes_da_primeira_entrada(app):
    from app.models import PlanejamentoItem, PlanejamentoItemBatelada, PlanejamentoProducao
    from app.services.viennoiserie import TIPO_MASSA
    from app.utils import hoje
    rec, ep = _massa()
    plano = PlanejamentoProducao(data=hoje(), enviado_ao_padeiro=True)
    item = PlanejamentoItem(receita=rec, qtd_alvo=1)
    item.batelada_padrao = PlanejamentoItemBatelada(
        dados={'tipo': TIPO_MASSA, 'unidades': 1, 'peso_bola_g': 3580}, bateladas=1)
    plano.itens.append(item)
    db.session.add(plano)
    rec.nome = 'Nome editado depois do envio'
    db.session.commit()
    assert SaldoResidualMassa.query.count() == 0
    assert eh_massa_folhar(rec)
    registrar_entrada_massa(rec, 44900, None, 'Primeira confirmação',
                            peso_bola_g_snapshot=3580)
    db.session.commit()
    assert _total_g(rec, ep) == Decimal(44900)


@pytest.mark.parametrize('operacao', ['entrada', 'consumo'])
def test_operacao_avulsa_antes_da_confirmacao_usa_unidade_da_primeira_ordem(app, operacao):
    from app.models import PlanejamentoItem, PlanejamentoItemBatelada, PlanejamentoProducao
    from app.services.viennoiserie import TIPO_MASSA
    from app.utils import hoje
    rec, ep = _massa(bolas=2)
    plano = PlanejamentoProducao(data=hoje(), enviado_ao_padeiro=True)
    item = PlanejamentoItem(receita=rec, qtd_alvo=1)
    item.batelada_padrao = PlanejamentoItemBatelada(
        dados={'tipo': TIPO_MASSA, 'peso_bola_g': 3580}, bateladas=1)
    plano.itens.append(item)
    db.session.add(plano)
    db.session.commit()
    rec.peso_unitario = 4000
    db.session.commit()
    assert peso_bola_g(rec) == Decimal(3580)
    assert SaldoResidualMassa.query.count() == 0
    if operacao == 'entrada':
        registrar_entrada_bolas(rec, 1, None, 'Entrada antes da confirmação')
        esperado = Decimal(10740)
    else:
        consumir_massa(rec, Decimal('0.5'), None, 'Montagem antes da confirmação')
        esperado = Decimal(5370)
    db.session.commit()
    assert _total_g(rec, ep) == esperado
    assert db.session.get(SaldoResidualMassa, rec.id).peso_bola_g == 3580
    registrar_entrada_massa(rec, 44900, None, 'Confirmação', peso_bola_g_snapshot=3580)
    db.session.commit()
    assert _total_g(rec, ep) == esperado + Decimal(44900)


def test_primeiro_snapshot_valido_fixa_peso_antes_de_outras_ordens(app):
    from datetime import datetime, timedelta

    from app.models import PlanejamentoItem, PlanejamentoItemBatelada, PlanejamentoProducao
    from app.services.viennoiserie import TIPO_MASSA
    from app.utils import hoje
    rec, ep = _massa()
    plano = PlanejamentoProducao(data=hoje(), enviado_ao_padeiro=True)
    for i, peso in enumerate((None, 3580, 4000)):
        item = PlanejamentoItem(receita=rec, qtd_alvo=1)
        item.batelada_padrao = PlanejamentoItemBatelada(
            dados={'tipo': TIPO_MASSA, 'peso_bola_g': peso}, bateladas=1,
            criado_em=datetime(2026, 9, 1) + timedelta(days=i))
        plano.itens.append(item)
    rec.peso_unitario = 4500
    db.session.add(plano)
    db.session.commit()
    assert peso_bola_g(rec) == Decimal(3580)
    # Mesmo uma ordem posterior não revaloriza a unidade já aprovada.
    registrar_entrada_massa(rec, 44900, None, 'Ordem posterior', peso_bola_g_snapshot=4000)
    db.session.commit()
    assert db.session.get(SaldoResidualMassa, rec.id).peso_bola_g == 3580
    assert _total_g(rec, ep) == Decimal(44900)


def test_identidade_historica_e_carregada_em_lote_sem_consulta_por_receita(app):
    from sqlalchemy import event
    rec, _ep = _massa()
    registrar_entrada_massa(rec, 44900, None, 'Batimento')
    rec.nome = 'Massa renomeada'
    outras = [Receita(nome=f'Receita comum {i}', rendimento_qtd=1,
                      rendimento_unidade='un', peso_base=1000) for i in range(5)]
    db.session.add_all(outras)
    db.session.commit()
    consultas = []

    def capturar(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith('SELECT') and (
                'saldo_residual_massa' in statement or 'planejamento_item_batelada' in statement):
            consultas.append(statement)

    event.listen(db.engine, 'before_cursor_execute', capturar)
    try:
        for _ in range(3):
            assert eh_massa_folhar(rec)
            assert not any(eh_massa_folhar(outra) for outra in outras)
    finally:
        event.remove(db.engine, 'before_cursor_execute', capturar)
    assert len(consultas) == 2


def test_cache_de_identidade_atualiza_depois_da_primeira_entrada(app):
    rec, ep = _massa()
    outra = Receita(nome='Outra receita', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=1000)
    db.session.add(outra)
    db.session.flush()
    assert not eh_massa_folhar(outra)  # carrega o conjunto ainda vazio
    registrar_entrada_massa(rec, 44900, None, 'Primeira entrada')
    rec.nome = 'Massa renomeada na mesma transação'
    db.session.flush()
    assert eh_massa_folhar(rec)
    assert _total_g(rec, ep) == Decimal(44900)
    db.session.rollback()
    assert not eh_massa_folhar(outra)  # objeto transitório após rollback
    assert SaldoResidualMassa.query.count() == 0


def test_cache_de_identidade_nao_sobrevive_rollback_de_savepoint(app):
    rec, _ep = _massa()
    rec.nome = 'Receita comum antes do savepoint'
    db.session.commit()
    assert not eh_massa_folhar(rec)
    savepoint = db.session.begin_nested()
    db.session.add(SaldoResidualMassa(receita_id=rec.id, g=0, peso_bola_g=3580))
    db.session.flush()
    assert eh_massa_folhar(rec)
    savepoint.rollback()
    assert db.session.get(SaldoResidualMassa, rec.id) is None
    assert not eh_massa_folhar(rec)


def test_snapshot_pendente_reconhece_identidade_sem_flush(app):
    from app.models import PlanejamentoItem, PlanejamentoItemBatelada, PlanejamentoProducao
    from app.services.viennoiserie import TIPO_MASSA
    from app.utils import hoje
    rec, _ep = _massa()
    rec.nome = 'Nome alterado'
    plano = PlanejamentoProducao(data=hoje())
    item = PlanejamentoItem(receita=rec, qtd_alvo=1)
    plano.itens.append(item)
    db.session.add(plano)
    db.session.flush()
    assert not eh_massa_folhar(rec)
    snapshot = PlanejamentoItemBatelada(dados={'tipo': TIPO_MASSA}, bateladas=1)
    item.batelada_padrao = snapshot
    db.session.add(snapshot)
    assert eh_massa_folhar(rec)
    assert snapshot in db.session.new


def test_estorno_conserva_massa_e_e_idempotente_sem_creditar_falta(app):
    rec, ep = _massa()
    registrar_entrada_massa(rec, 1790, None, 'Meia bola')
    consumir_massa(rec, 1, None, 'Perda parcial', tipo='perda_producao')
    db.session.commit()
    saidas = MovEstoqueMassa.query.filter(
        MovEstoqueMassa.tipo.in_(('perda_producao', 'perda_producao_sem_estoque'))).all()
    ids = [m.id for m in saidas]
    out = estornar_movimentos_massa(rec, ids, None, 'Estorno real')
    assert out == {'estornado': Decimal('0.5'), 'estornado_g': Decimal(1790)}
    assert _total_g(rec, ep) == Decimal(1790)
    # Repete antes do commit para testar também a visibilidade na transação.
    assert estornar_movimentos_massa(rec, ids, None, 'Retry')['estornado_g'] == 0
    db.session.commit()
    assert _total_g(rec, ep) == Decimal(1790)
    estorno = MovEstoqueMassa.query.filter_by(tipo='perda_producao_estorno').one()
    assert estorno.estorno_de_id == next(m.id for m in saidas if m.tipo == 'perda_producao')
    assert estorno.quantidade_g == Decimal(1790)


def test_rollback_do_estorno_preserva_saida_e_permite_repetir(app):
    rec, ep = _massa(bolas=2)
    consumir_massa(rec, Decimal('0.25'), None, 'Consumo')
    db.session.commit()
    saida_id = MovEstoqueMassa.query.one().id
    estornar_movimentos_massa(rec, [saida_id], None, 'Estorno')
    db.session.flush()
    db.session.rollback()
    assert _total_g(rec, ep) == Decimal(6265)
    assert MovEstoqueMassa.query.filter_by(estorno_de_id=saida_id).count() == 0
    estornar_movimentos_massa(rec, [saida_id], None, 'Estorno definitivo')
    db.session.commit()
    assert _total_g(rec, ep) == Decimal(7160)


def test_estorno_recusa_movimento_de_entrada(app):
    rec, ep = _massa()
    registrar_entrada_massa(rec, 44900, None, 'Batimento')
    db.session.commit()
    entrada_id = MovEstoqueMassa.query.one().id
    with pytest.raises(ValueError, match='movimentos de saída'):
        estornar_movimentos_massa(rec, [entrada_id], None, 'Crédito duplicado')
    db.session.rollback()
    assert _total_g(rec, ep) == Decimal(44900)


@pytest.mark.parametrize('entrada_g, perda_bolas', [(1790, 1), (44900, 13), (44900, 1)])
def test_excluir_perda_de_massa_estorna_gramas_e_nao_duplica_inteiros(
        app, admin_user, entrada_g, perda_bolas):
    from app.models import Funcionario, PerdaProducao
    from app.services import perda_producao
    rec, ep = _massa()
    funcionario = Funcionario(nome='Padeiro da massa', cpf='111.111.111-11',
                              ativo=True, funcao='Padeiro')
    db.session.add(funcionario)
    registrar_entrada_massa(rec, entrada_g, admin_user.id, 'Entrada medida')
    db.session.commit()
    resultado = perda_producao.registrar(
        rec.id, perda_bolas, 'caiu', admin_user.id, funcionario_id=funcionario.id)
    quantidade_real = min(Decimal(entrada_g), Decimal(perda_bolas) * Decimal(3580))
    assert _total_g(rec, ep) == Decimal(entrada_g) - quantidade_real
    assert MovEstoqueMassa.query.filter_by(perda_producao_id=resultado['perda_id']).count() > 0
    estorno = perda_producao.excluir(resultado['perda_id'], admin_user.id)
    assert estorno['estornado'] == quantidade_real / Decimal(3580)
    assert _total_g(rec, ep) == Decimal(entrada_g)
    assert PerdaProducao.query.count() == 0
    assert MovEstoqueMassa.query.filter(MovEstoqueMassa.perda_producao_id.isnot(None)).count() == 0
    assert MovEstoqueMassa.query.filter_by(tipo='perda_producao_estorno').one().quantidade_g == quantidade_real
    with pytest.raises(ValueError, match='não encontrada'):
        perda_producao.excluir(resultado['perda_id'], admin_user.id)
    assert _total_g(rec, ep) == Decimal(entrada_g)

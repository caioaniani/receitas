"""Entradas, perdas e contagens da indústria conservam a fração de massa."""

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
from app.services.estoque_congelados import entrada_producao, saida_producao
from app.services.estoque_massa import registrar_entrada_massa, saldo_bolas


def _massa(*, produzir=True):
    rec = Receita(nome='Massa para folhar', categoria='Viennoiserie',
                  peso_base=2000, peso_unitario=3580, rendimento_qtd=1,
                  rendimento_unidade='un')
    db.session.add(rec)
    db.session.flush()
    if produzir:
        registrar_entrada_massa(rec, 44900, None, 'Batimento compartilhado')
    db.session.commit()
    ep = EstoqueProducao.query.filter_by(receita_id=rec.id).first()
    return rec, ep


def _cliente(app, admin_user):
    cliente = app.test_client()
    with cliente.session_transaction() as sess:
        sess['_user_id'] = str(admin_user.id)
        sess['_fresh'] = True
    return cliente


def _total(rec):
    resto = db.session.get(SaldoResidualMassa, rec.id)
    bolas = sum(ep.quantidade for ep in EstoqueProducao.query.filter_by(receita_id=rec.id))
    return Decimal(bolas) * resto.peso_bola_g + resto.g


def test_entrada_generica_em_bolas_preserva_resto_e_peso_congelado(app, admin_user):
    rec, ep = _massa()
    rec.peso_unitario = 4000
    db.session.commit()
    retornado = entrada_producao(receita_id=rec.id, quantidade=2, usuario_id=admin_user.id)
    db.session.commit()
    assert retornado.id == ep.id
    assert _total(rec) == Decimal(52060)
    assert ep.quantidade == 14
    assert db.session.get(SaldoResidualMassa, rec.id).g == Decimal(1940)
    assert MovEstoqueMassa.query.filter_by(tipo='producao').count() == 2
    assert MovEstoqueProducao.query.filter_by(tipo='producao').count() == 2


def test_saida_generica_fracionaria_nao_trunca_para_zero(app, admin_user):
    rec, _ep = _massa()
    resultado = saida_producao(receita_id=rec.id, quantidade=Decimal('0.25'),
                              usuario_id=admin_user.id, referencia='Montagem')
    db.session.commit()
    assert resultado['baixado_g'] == Decimal(895)
    assert resultado['falta'] == 0
    assert _total(rec) == Decimal(44005)
    assert MovEstoqueMassa.query.filter_by(tipo='consumo_subreceita').count() == 1


def test_entrada_consolida_duplicatas_e_preserva_vinculos_do_historico(app, admin_user):
    rec, ep = _massa()
    extra = EstoqueProducao(receita_id=rec.id, quantidade=1, estado='backup')
    db.session.add(extra)
    db.session.flush()
    mov = MovEstoqueProducao(estoque_producao_id=extra.id, tipo='producao', quantidade=1)
    db.session.add(mov)
    db.session.flush()
    exato = MovEstoqueMassa(receita_id=rec.id, estoque_producao_id=extra.id,
                            movimento_inteiro_id=mov.id, tipo='producao',
                            quantidade_g=3580, saldo_anterior_g=0, saldo_posterior_g=3580,
                            peso_bola_g=3580)
    db.session.add(exato)
    db.session.commit()
    entrada_producao(receita_id=rec.id, quantidade=1, usuario_id=admin_user.id)
    db.session.commit()
    assert EstoqueProducao.query.filter_by(receita_id=rec.id).count() == 1
    assert _total(rec) == Decimal(52060)
    assert exato.estoque_producao_id == mov.estoque_producao_id == ep.id


def test_rota_entrada_e_perda_em_bolas_preservam_residual(app, admin_user):
    rec, ep = _massa()
    cliente = _cliente(app, admin_user)
    resposta = cliente.post('/pedidos/congelados/entrada', data={
        'tipo': 'receita', 'item_id': rec.id, 'quantidade': 2})
    assert resposta.status_code == 302
    assert _total(rec) == Decimal(52060)
    resposta = cliente.post('/pedidos/congelados/ajuste', data={
        'estoque_id': ep.id, 'quantidade': 1, 'tipo_ajuste': 'perda', 'referencia': 'Caiu'})
    assert resposta.status_code == 302
    assert _total(rec) == Decimal(48480)
    assert db.session.get(SaldoResidualMassa, rec.id).g == Decimal(1940)
    assert MovEstoqueMassa.query.filter_by(tipo='perda').one().quantidade_g == 3580
    assert MovEstoqueProducao.query.filter_by(tipo='perda').count() == 1


def test_ajuste_que_excede_saldo_registra_deficit_exato(app, admin_user):
    rec, ep = _massa()
    resposta = _cliente(app, admin_user).post('/pedidos/congelados/ajuste', data={
        'estoque_id': ep.id, 'quantidade': 13, 'tipo_ajuste': 'ajuste'})
    assert resposta.status_code == 302
    assert _total(rec) == 0
    assert MovEstoqueMassa.query.filter_by(tipo='ajuste').one().quantidade_g == 44900
    assert MovEstoqueMassa.query.filter_by(tipo='ajuste_sem_estoque').one().quantidade_g == 1640


@pytest.mark.parametrize('origem', ['balanco', 'conferencia', 'item_adicionado'])
def test_contagem_absoluta_mantendo_inteiro_limpa_resto_e_eh_idempotente(
        app, admin_user, origem):
    rec, ep = _massa()
    cliente = _cliente(app, admin_user)
    if origem == 'balanco':
        url, dados = '/pedidos/congelados/balanco/aplicar', {'texto': 'Massa para folhar: 12'}
    elif origem == 'conferencia':
        url, dados = '/pedidos/congelados/conferencia', {f'real_{ep.id}': '12'}
    else:
        url, dados = '/pedidos/congelados/conferencia', {
            'novo_alvo': f'receita:{rec.id}', 'novo_qtd': '12'}
    assert cliente.post(url, data=dados).status_code == 302
    assert _total(rec) == 42960
    assert ep.quantidade == 12
    ajuste = MovEstoqueMassa.query.filter_by(tipo='ajuste_conferencia').one()
    assert ajuste.quantidade_g == 1940
    assert ajuste.movimento_inteiro_id is None
    assert cliente.post(url, data=dados).status_code == 302
    assert MovEstoqueMassa.query.filter_by(tipo='ajuste_conferencia').count() == 1


def test_conferencia_adiciona_massa_sem_linha_de_estoque(app, admin_user):
    rec, ep = _massa(produzir=False)
    assert ep is None
    resposta = _cliente(app, admin_user).post('/pedidos/congelados/conferencia', data={
        'novo_alvo': f'receita:{rec.id}', 'novo_qtd': '2'})
    assert resposta.status_code == 302
    assert _total(rec) == 7160
    assert saldo_bolas(rec) == 2


def test_conferencia_zero_confirma_conversao_de_debito_legado(app, admin_user):
    rec, _ep = _massa(produzir=False)
    ep = EstoqueProducao(receita_id=rec.id, quantidade=0)
    db.session.add_all([ep, ConsumoSubFracao(receita_id=rec.id, fracao_pendente=0.25)])
    db.session.commit()
    resposta = _cliente(app, admin_user).post('/pedidos/congelados/conferencia', data={
        f'real_{ep.id}': '0'})
    assert resposta.status_code == 302
    assert ConsumoSubFracao.query.one().fracao_pendente == 0
    assert MovEstoqueMassa.query.filter_by(
        tipo='consumo_subreceita_sem_estoque').one().quantidade_g == 895


def test_vinculacao_soma_bolas_preservando_sobra_e_historico(app, admin_user):
    rec, ep = _massa()
    pendente = EstoqueProducao(nome_pendente='Massa antiga', quantidade=2)
    db.session.add(pendente)
    db.session.flush()
    mov = MovEstoqueProducao(estoque_producao_id=pendente.id,
                            tipo='balanco_entrada', quantidade=2)
    db.session.add(mov)
    db.session.commit()
    resposta = _cliente(app, admin_user).post('/pedidos/congelados/vincular', data={
        'estoque_id': pendente.id, 'alvo_tipo': 'receita', 'alvo_id': rec.id})
    assert resposta.status_code == 302
    assert _total(rec) == 52060
    assert mov.estoque_producao_id == ep.id
    assert EstoqueProducao.query.filter_by(nome_pendente='Massa antiga').count() == 0


def test_leituras_exibem_saldo_exato_sem_gravar_e_outros_itens_continuam_inteiros(
        app, admin_user):
    rec, ep = _massa()
    outra = Receita(nome='Danish de maçã', peso_base=1000, rendimento_qtd=1,
                    rendimento_unidade='un')
    db.session.add(outra)
    db.session.flush()
    outro = entrada_producao(receita_id=outra.id, quantidade=7, usuario_id=admin_user.id)
    db.session.commit()
    antes = MovEstoqueMassa.query.count()
    cliente = _cliente(app, admin_user)
    for url in ('/pedidos/congelados', '/pedidos/congelados/conferencia'):
        resposta = cliente.get(url)
        assert resposta.status_code == 200
        assert '44.900,000 g' in resposta.get_data(as_text=True)
    assert MovEstoqueMassa.query.count() == antes
    assert cliente.post('/pedidos/congelados/conferencia', data={
        f'real_{ep.id}': '12', f'real_{outro.id}': '7'}).status_code == 302
    assert outro.quantidade == 7
    assert MovEstoqueProducao.query.filter_by(
        estoque_producao_id=outro.id, tipo='ajuste_conferencia').count() == 0


def test_peso_ausente_exibe_erro_sem_impedir_leitura_do_estoque(app, admin_user):
    rec, _ep = _massa(produzir=False)
    rec.peso_unitario = None
    db.session.add(EstoqueProducao(receita_id=rec.id, quantidade=2))
    db.session.commit()
    cliente = _cliente(app, admin_user)
    for url in ('/pedidos/congelados', '/pedidos/congelados/conferencia'):
        resposta = cliente.get(url)
        assert resposta.status_code == 200
        assert 'Peso da bola: quantidade inválida.' in resposta.get_data(as_text=True)
    assert SaldoResidualMassa.query.count() == MovEstoqueMassa.query.count() == 0

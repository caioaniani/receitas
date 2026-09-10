"""Registros factuais, correções auditadas e concorrência independente da ordem."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.extensions import db
from app.models import (
    MassaBase,
    MassaBaseItem,
    PlanejamentoItem,
    PlanejamentoProducao,
    ProducaoDiarioAlteracao,
    ProducaoDiarioEtapa,
    ProducaoDiarioLote,
    Receita,
)
from app.services.producao_diario import (
    DiarioConflito,
    DiarioError,
    adicionar_etapa,
    concluir_etapa,
    corrigir_etapa,
    criar_lote,
    definir_status,
    obter_origem,
    origens_do_dia,
    salvar_lote,
)
from app.utils import agora, hoje


@pytest.fixture(autouse=True)
def _relogio(congela_hoje):
    congela_hoje(2026, 9, 10, 10)


@pytest.fixture
def origem(admin_user):
    receita = Receita(nome='Sourdough Tradicional', categoria='Paes',
                      rendimento_qtd=12, rendimento_unidade='un', peso_base=1000)
    plano = PlanejamentoProducao(data=hoje(), origem='cronograma', enviado_ao_padeiro=True)
    db.session.add_all([receita, plano])
    db.session.flush()
    item = PlanejamentoItem(planejamento_id=plano.id, receita_id=receita.id,
                            qtd_alvo=12, produzido_qtd=0)
    base = MassaBase(nome='Massa sourdough')
    db.session.add_all([item, base])
    db.session.flush()
    db.session.add(MassaBaseItem(massa_base_id=base.id, receita_id=receita.id))
    db.session.commit()
    return dict(item=item, plano=plano, receita=receita, base=base, usuario=admin_user)


def _criar(origem, **kwargs):
    dados = dict(tipo='item', origem_id=origem['item'].id, dia=hoje(),
                 data_producao=hoje(), identificacao='', chave=str(uuid4()),
                 usuario_id=origem['usuario'].id)
    dados.update(kwargs)
    return criar_lote(**dados)


def _etapa(lote, origem, **kwargs):
    dados = dict(lote_id=lote.id, nome='Batimento', inicio=agora() - timedelta(minutes=20),
                 fim=None, observacao='', usuario_id=origem['usuario'].id, versao=lote.versao)
    dados.update(kwargs)
    return adicionar_etapa(**dados)


def test_lista_origens_so_de_ordem_cronograma_enviada_do_dia(origem):
    listado = origens_do_dia(hoje())
    assert {(x['tipo'], x['id']) for x in listado} == {
        ('item', origem['item'].id), ('base', origem['base'].id)}
    assert obter_origem('base', origem['base'].id, hoje())['planejamento_id'] == origem['plano'].id
    assert origens_do_dia(hoje() + timedelta(days=1)) == []
    for atributo, valor in [('enviado_ao_padeiro', False), ('origem', 'manual')]:
        antigo = getattr(origem['plano'], atributo)
        setattr(origem['plano'], atributo, valor)
        db.session.commit()
        assert origens_do_dia(hoje()) == []
        with pytest.raises(DiarioError):
            _criar(origem)
        setattr(origem['plano'], atributo, antigo)
        db.session.commit()


def test_lote_pode_comecar_sem_medidas_ou_etapas_e_criacao_e_idempotente(origem):
    chave = str(uuid4())
    lote = _criar(origem, chave=chave)
    repetido = _criar(origem, chave=chave)
    assert repetido.id == lote.id
    assert lote.medidas == {} and lote.etapas == []
    assert lote.nome == origem['receita'].nome
    assert lote.receita_id == origem['receita'].id
    assert len(lote.alteracoes) == 1
    assert lote.alteracoes[0].acao == 'criar_lote'
    assert lote.alteracoes[0].depois['medidas'] == {}
    with pytest.raises(DiarioConflito):
        _criar(origem, chave=chave, tipo='base', origem_id=origem['base'].id)
    assert ProducaoDiarioLote.query.count() == 1


@pytest.mark.parametrize('dados', [
    {'chave': 'invalida'}, {'origem_id': 999999}, {'tipo': 'receita'},
    {'data_producao': '2026-09-11'}, {'data_producao': '32/09/2026'},
    {'identificacao': 'x' * 101}, {'origem_id': True},
])
def test_criacao_invalida_nao_grava(origem, dados):
    with pytest.raises(DiarioError):
        _criar(origem, **dados)
    assert ProducaoDiarioLote.query.count() == 0
    assert ProducaoDiarioAlteracao.query.count() == 0


def test_ordem_de_amanha_pode_ser_medida_hoje(origem):
    origem['plano'].data = hoje() + timedelta(days=1)
    db.session.commit()
    lote = _criar(origem, dia=origem['plano'].data)
    assert lote.data_ordem > lote.data_producao


def test_medidas_ptbr_zero_agua_limites_e_auditoria_antes_depois(origem):
    lote = _criar(origem)
    salvar_lote(lote.id, {'farinha_kg': '1.234,5', 'agua_kg': '0', 'massa_kg': '',
                          'temperatura_agua': '-30', 'temperatura_massa': '150',
                          'observacao': 'Massa fria'}, origem['usuario'].id, lote.versao)
    assert lote.medidas == {'farinha_kg': 1234.5, 'agua_kg': 0.0,
                            'temperatura_agua': -30.0, 'temperatura_massa': 150.0}
    salvar_lote(lote.id, {'farinha_kg': '', 'temperatura_agua': '12,5'},
                 origem['usuario'].id, lote.versao)
    alteracao = lote.alteracoes[-1]
    assert alteracao.antes['medidas']['farinha_kg'] == 1234.5
    assert 'farinha_kg' not in alteracao.depois['medidas']
    assert lote.medidas['temperatura_agua'] == 12.5
    assert alteracao.usuario_id == origem['usuario'].id


@pytest.mark.parametrize('campo,valor', [
    ('farinha_kg', 'NaN'), ('agua_kg', 'inf'), ('massa_kg', '-inf'),
    ('farinha_kg', '0'), ('massa_kg', '-1'), ('agua_kg', '-0,1'),
    ('temperatura_agua', '-30,1'), ('temperatura_massa', '150.1'),
    ('temperatura_farinha', 'texto'), ('observacao', 'x' * 4001),
])
def test_medidas_invalidas_nao_alteram_versao_nem_auditoria(origem, campo, valor):
    lote = _criar(origem)
    versao = lote.versao
    with pytest.raises(DiarioError):
        salvar_lote(lote.id, {campo: valor}, origem['usuario'].id, versao)
    db.session.refresh(lote)
    assert lote.versao == versao and lote.medidas == {}
    assert len(lote.alteracoes) == 1


def test_versao_antiga_nao_sobrescreve_medidas_e_nao_duplica_etapa(origem):
    lote = _criar(origem)
    versao = lote.versao
    salvar_lote(lote.id, {'farinha_kg': '2'}, origem['usuario'].id, versao)
    with pytest.raises(DiarioConflito):
        salvar_lote(lote.id, {'farinha_kg': '3'}, origem['usuario'].id, versao)
    assert lote.medidas['farinha_kg'] == 2
    versao = lote.versao
    _etapa(lote, origem, versao=versao)
    with pytest.raises(DiarioConflito):
        _etapa(lote, origem, versao=versao)
    assert ProducaoDiarioEtapa.query.count() == 1


def test_etapas_simultaneas_e_fermentacao_overnight_sem_automaticos(origem):
    lote = _criar(origem, data_producao=hoje() - timedelta(days=1))
    fria = _etapa(lote, origem, nome='Frio', inicio='2026-09-09T18:00', fim='2026-09-10T06:00')
    preparo = _etapa(lote, origem, nome='Preparo', inicio='2026-09-09T19:00', fim='2026-09-09T19:10')
    assert fria.duracao_min == 720
    assert preparo.duracao_min == 10
    aberta = _etapa(lote, origem)
    assert aberta.duracao_min is None
    assert len(lote.etapas) == 3


@pytest.mark.parametrize('dados', [
    {'nome': ''}, {'nome': 'x' * 101}, {'inicio': ''},
    {'inicio': '2026-09-10T10:06'}, {'fim': '2026-09-10T10:06'},
    {'inicio': '2026-09-10T09:00', 'fim': '2026-09-10T08:00'},
    {'inicio': '2026-09-10T09:00-03:00'}, {'inicio': 'inválido'},
])
def test_etapa_invalida_nao_grava(origem, dados):
    lote = _criar(origem)
    with pytest.raises(DiarioError):
        _etapa(lote, origem, **dados)
    assert lote.versao == 1
    assert ProducaoDiarioEtapa.query.count() == 0


def test_conclusao_etapa_idempotente_e_owned_pelo_lote(origem):
    lote, outro = _criar(origem), _criar(origem)
    etapa = _etapa(lote, origem)
    with pytest.raises(DiarioError):
        concluir_etapa(outro.id, etapa.id, origem['usuario'].id)
    assert etapa.fim_em is None
    concluir_etapa(lote.id, etapa.id, origem['usuario'].id)
    versao, auditorias = lote.versao, len(lote.alteracoes)
    concluir_etapa(lote.id, etapa.id, origem['usuario'].id)
    assert lote.versao == versao and len(lote.alteracoes) == auditorias
    assert etapa.fim_em == agora()


def test_concluir_reabrir_corrigir_e_medidas_tardias_auditadas(origem):
    lote = _criar(origem)
    etapa = _etapa(lote, origem)
    versao = lote.versao
    with pytest.raises(DiarioError, match='em andamento'):
        definir_status(lote.id, 'concluido', origem['usuario'].id, versao)
    assert lote.versao == versao and lote.status == 'aberto'
    concluir_etapa(lote.id, etapa.id, origem['usuario'].id)
    definir_status(lote.id, 'concluido', origem['usuario'].id, lote.versao)
    salvar_lote(lote.id, {'temperatura_massa': '27'}, origem['usuario'].id, lote.versao)
    corrigir_etapa(lote.id, etapa.id, '2026-09-10T09:35', '2026-09-10T09:59',
                   'Horário corrigido no fim do turno', origem['usuario'].id, lote.versao,
                   nome='Batimento na velocidade 1')
    assert etapa.duracao_min == 24
    alteracao = lote.alteracoes[-1]
    assert alteracao.acao == 'corrigir_etapa'
    assert alteracao.antes['inicio_em'] == '2026-09-10T09:40:00'
    assert alteracao.depois['inicio_em'] == '2026-09-10T09:35:00'
    assert alteracao.antes['nome'] == 'Batimento'
    assert alteracao.depois['nome'] == 'Batimento na velocidade 1'
    with pytest.raises(DiarioError):
        _etapa(lote, origem)
    with pytest.raises(DiarioError, match='Reabra'):
        corrigir_etapa(lote.id, etapa.id, etapa.inicio_em, None, '', origem['usuario'].id, lote.versao)
    definir_status(lote.id, 'aberto', origem['usuario'].id, lote.versao)
    _etapa(lote, origem, nome='Resfriamento')
    assert lote.status == 'aberto'


def test_apagar_planejamento_e_base_preserva_snapshot_etapas_auditoria(origem):
    lote = _criar(origem, tipo='base', origem_id=origem['base'].id)
    _etapa(lote, origem)
    db.session.delete(origem['plano'])
    db.session.delete(origem['base'])
    db.session.commit()
    db.session.expire_all()
    assert lote.nome == 'Massa sourdough'
    assert lote.receita_id is None
    assert len(lote.etapas) == 1 and len(lote.alteracoes) == 2
    salvar_lote(lote.id, {'massa_kg': '15'}, origem['usuario'].id, lote.versao)
    assert lote.medidas['massa_kg'] == 15


def test_diario_nunca_emite_writes_em_estoque_receita_ou_plano(origem):
    writes = []

    def observar(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE'):
            writes.append(statement)

    event.listen(db.engine, 'before_cursor_execute', observar)
    try:
        lote = _criar(origem)
        salvar_lote(lote.id, {'farinha_kg': '15'}, origem['usuario'].id, lote.versao)
        etapa = _etapa(lote, origem)
        concluir_etapa(lote.id, etapa.id, origem['usuario'].id)
        definir_status(lote.id, 'concluido', origem['usuario'].id, lote.versao)
    finally:
        event.remove(db.engine, 'before_cursor_execute', observar)
    assert writes and all('producao_diario_' in sql for sql in writes)
    assert origem['item'].produzido_qtd == 0


def test_dois_workers_mesma_versao_so_um_adiciona_etapa(app, origem):
    lote = _criar(origem)
    lote_id, usuario_id, versao = lote.id, origem['usuario'].id, lote.versao
    barreira = Barrier(2)

    def registrar():
        with app.app_context():
            barreira.wait(timeout=10)
            try:
                adicionar_etapa(lote_id, 'Dobra 1', '2026-09-10T09:00', None, '', usuario_id, versao)
                return 'salvo'
            except DiarioConflito:
                return 'conflito'
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as pool:
        resultados = list(pool.map(lambda _: registrar(), range(2)))
    db.session.expire_all()
    assert sorted(resultados) == ['conflito', 'salvo']
    assert ProducaoDiarioEtapa.query.count() == 1
    assert lote.versao == 2


def test_dois_workers_mesma_chave_criam_um_lote(app, origem):
    item_id, usuario_id, chave = origem['item'].id, origem['usuario'].id, str(uuid4())
    barreira = Barrier(2)

    def registrar():
        with app.app_context():
            barreira.wait(timeout=10)
            try:
                return criar_lote('item', item_id, hoje(), hoje(), '', chave, usuario_id).id
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as pool:
        resultados = list(pool.map(lambda _: registrar(), range(2)))
    assert resultados[0] == resultados[1]
    assert ProducaoDiarioLote.query.count() == 1
    assert ProducaoDiarioAlteracao.query.count() == 1


def test_dois_workers_concluem_etapa_uma_so_vez(app, origem):
    lote = _criar(origem)
    etapa = _etapa(lote, origem)
    lote_id, etapa_id, usuario_id = lote.id, etapa.id, origem['usuario'].id
    barreira = Barrier(2)

    def registrar():
        with app.app_context():
            barreira.wait(timeout=10)
            try:
                return concluir_etapa(lote_id, etapa_id, usuario_id).fim_em
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as pool:
        resultados = list(pool.map(lambda _: registrar(), range(2)))
    assert resultados == [agora(), agora()]
    assert ProducaoDiarioAlteracao.query.filter_by(evento='concluir_etapa').count() == 1
    db.session.refresh(lote)
    assert lote.versao == 3

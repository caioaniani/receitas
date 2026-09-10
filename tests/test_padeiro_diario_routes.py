"""Registro incremental pela tela, com histórico e sem lançar estoque."""
import csv
import io
import re
from datetime import date
from uuid import uuid4

import pytest

from app.extensions import db
from app.models import (
    EstoqueProducao,
    MassaBase,
    MassaBaseItem,
    MovEstoqueProducao,
    PlanejamentoItem,
    PlanejamentoProducao,
    PreBaixaMP,
    ProducaoDiarioLote,
    Receita,
    ReceitaEtapa,
    Usuario,
)

DIA = date(2026, 9, 10)


@pytest.fixture(autouse=True)
def _tempo(congela_hoje):
    congela_hoje(2026, 9, 10, hora=16)


@pytest.fixture
def item(app):
    receita = Receita(nome='Sourdough do diário', categoria='Pães',
                      rendimento_qtd=10, rendimento_unidade='un', peso_base=1000)
    db.session.add(receita)
    db.session.flush()
    db.session.add(ReceitaEtapa(receita_id=receita.id, nome='Dobra 1',
                               ordem=1, duracao_min=5, ativa=True))
    plano = PlanejamentoProducao(data=DIA, origem='cronograma',
                                status='aprovado', enviado_ao_padeiro=True)
    db.session.add(plano)
    db.session.flush()
    item = PlanejamentoItem(planejamento_id=plano.id, receita_id=receita.id,
                           multiplicador=1, qtd_alvo=10, produzido_qtd=0)
    db.session.add(item)
    db.session.commit()
    return item


def _login(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


def _criar(client, item, **extra):
    data = dict(origem=f'item:{item.id}', data_ordem=DIA.isoformat(),
                data_producao=DIA.isoformat(), identificacao='Batida 1', chave=str(uuid4()))
    data.update(extra)
    resposta = client.post('/padeiro/diario/lotes', data=data)
    assert resposta.status_code == 302
    return ProducaoDiarioLote.query.order_by(ProducaoDiarioLote.id.desc()).first()


def _post(client, lote, caminho, **dados):
    dados.setdefault('versao', lote.versao)
    r = client.post(f'/padeiro/diario/lotes/{lote.id}/{caminho}', data=dados)
    db.session.expire_all()
    return r


def test_fluxo_incremental_medicoes_horarios_historico_sem_estoque(app, admin_user, item):
    client = _login(app, admin_user)
    estoques_antes = (EstoqueProducao.query.count(), MovEstoqueProducao.query.count(),
                     PreBaixaMP.query.count())
    html = client.get('/padeiro/gantt?data=2026-09-10').get_data(as_text=True)
    assert 'Diário de produção' in html and 'Registrar lote' in html
    lote = _criar(client, item)
    assert not lote.etapas
    resposta = client.get(f'/padeiro/diario/lotes/{lote.id}')
    assert resposta.status_code == 200
    assert 'Iniciar etapa' in resposta.get_data(as_text=True)
    assert _post(client, lote, 'medidas', farinha_kg='25', agua_kg='20',
                 temperatura_massa='21,8', observacao='Massa elástica').status_code == 302
    assert float(lote.medidas['temperatura_massa']) == 21.8
    assert 'temperatura_farinha' not in lote.medidas or lote.medidas['temperatura_farinha'] is None
    assert _post(client, lote, 'etapas', nome='Velocidade 1', modo='horarios',
                 inicio_em='2026-09-10T08:00', fim_em='2026-09-10T08:18').status_code == 302
    etapa = lote.etapas[0]
    assert etapa.duracao_min == 18
    assert _post(client, lote, 'status', status='concluido').status_code == 302
    assert client.get('/padeiro/diario/historico?inicio=2026-09-10&fim=2026-09-10').status_code == 200
    rows = list(csv.reader(io.StringIO(client.get(
        '/padeiro/diario/exportar.csv?inicio=2026-09-10&fim=2026-09-10'
    ).get_data(as_text=True).lstrip('\ufeff')), delimiter=';'))
    assert len(rows) == 2 and 'Velocidade 1' in rows[1]
    assert db.session.get(PlanejamentoItem, item.id).produzido_qtd == 0
    assert estoques_antes == (EstoqueProducao.query.count(), MovEstoqueProducao.query.count(),
                             PreBaixaMP.query.count())


def test_erro_preserva_medida_digitada_e_conflito_nao_sobrescreve(app, admin_user, item):
    client = _login(app, admin_user)
    lote = _criar(client, item)
    versao = lote.versao
    assert _post(client, lote, 'medidas', farinha_kg='25').status_code == 302
    r = _post(client, lote, 'medidas', farinha_kg='30', versao=versao)
    assert r.status_code == 409
    assert float(lote.medidas['farinha_kg']) == 25
    assert '30' in r.get_data(as_text=True)
    r = _post(client, lote, 'medidas', farinha_kg='abc', observacao='Conservar este texto')
    assert r.status_code == 400
    assert 'Conservar este texto' in r.get_data(as_text=True)


def test_overnight_correcao_historico_e_lote_persistem(app, admin_user, item):
    client = _login(app, admin_user)
    lote = _criar(client, item, data_producao='2026-09-09')
    assert _post(client, lote, 'etapas', nome='Fermentação', modo='horarios',
                 inicio_em='2026-09-09T18:00', fim_em='2026-09-10T06:00').status_code == 302
    etapa = lote.etapas[0]
    assert etapa.duracao_min == 720
    assert _post(client, lote, f'etapas/{etapa.id}/corrigir',
                 inicio_em='2026-09-09T18:30', fim_em='2026-09-10T06:00',
                 observacao='Horário conferido no caderno').status_code == 302
    assert etapa.duracao_min == 690
    assert lote.alteracoes
    # A reconstrução futura da ordem não apaga as observações feitas no lote.
    db.session.delete(item)
    db.session.commit()
    assert client.get(f'/padeiro/diario/lotes/{lote.id}').status_code == 200


def test_etapa_de_outro_lote_e_recusada(app, admin_user, item):
    client = _login(app, admin_user)
    a = _criar(client, item)
    b = _criar(client, item)
    assert _post(client, a, 'etapas', nome='Dobra', modo='agora').status_code == 302
    etapa = a.etapas[0]
    r = _post(client, b, f'etapas/{etapa.id}/concluir')
    assert r.status_code in (400, 404)
    assert etapa.fim_em is None


def test_corrigir_nome_e_observacao_preserva_segundos_medidos(app, admin_user, item):
    client = _login(app, admin_user)
    lote = _criar(client, item)
    assert _post(client, lote, 'etapas', nome='Batimento', modo='horarios',
                 inicio_em='2026-09-10T10:00:59.123456',
                 fim_em='2026-09-10T10:02:01.654321').status_code == 302
    etapa = lote.etapas[0]
    html = client.get(f'/padeiro/diario/lotes/{lote.id}').get_data(as_text=True)
    # Reenviar os valores efetivamente mostrados pelo formulário de correção.
    inicio = re.search(rf'id="inicio-{etapa.id}"[^>]*value="([^"]*)"', html).group(1)
    fim = re.search(rf'id="fim-{etapa.id}"[^>]*value="([^"]*)"', html).group(1)
    assert _post(client, lote, f'etapas/{etapa.id}/corrigir', nome='Velocidade 2',
                 inicio_em=inicio, fim_em=fim, observacao='Massa no ponto').status_code == 302
    assert etapa.nome == 'Velocidade 2'
    assert (etapa.fim_em - etapa.inicio_em).total_seconds() == 62


def test_lote_base_e_disponivel_com_origem_identificada(app, admin_user, item):
    base = MassaBase(nome='Base Sourdough')
    db.session.add(base)
    db.session.flush()
    db.session.add(MassaBaseItem(massa_base_id=base.id, receita_id=item.receita_id, ordem=0))
    db.session.commit()
    client = _login(app, admin_user)
    r = client.get(f'/padeiro/diario?data=2026-09-10&origem=base:{base.id}')
    assert r.status_code == 200 and 'Base Sourdough' in r.get_data(as_text=True)
    lote = _criar(client, item, origem=f'base:{base.id}')
    assert lote.tipo_origem == 'base' and lote.nome == 'Base Sourdough'


@pytest.mark.parametrize('papel,liberado', [('padeiro', True), ('producao', True),
                                         ('funcionario', False), ('gerente', False)])
def test_acesso_segue_permissao_da_tela(app, item, papel, liberado):
    user = Usuario(nome='Operador', login=f'diario-{papel}', papel=papel)
    user.set_senha('123')
    db.session.add(user)
    db.session.commit()
    client = _login(app, user)
    esperado = 200 if liberado else 403
    assert client.get('/padeiro/diario').status_code == esperado
    assert client.get('/padeiro/diario/historico').status_code == esperado
    if liberado:
        _criar(client, item)
    else:
        assert client.post('/padeiro/diario/lotes', data={}).status_code == 403


def test_get_nao_cria_lotes_e_post_exige_csrf(app, admin_user, item):
    client = _login(app, admin_user)
    for _ in range(2):
        assert client.get('/padeiro/diario?data=2026-09-10').status_code == 200
    assert ProducaoDiarioLote.query.count() == 0
    app.config['WTF_CSRF_ENABLED'] = True
    # O handler HTML global redireciona com aviso quando falta CSRF.
    assert client.post('/padeiro/diario/lotes', data={}).status_code == 302
    assert ProducaoDiarioLote.query.count() == 0


def test_csv_neutraliza_formula_e_preserva_celula_vazia(app, admin_user, item):
    client = _login(app, admin_user)
    lote = _criar(client, item, identificacao='=2+2')
    _post(client, lote, 'medidas', observacao='@comando')
    resposta = client.get('/padeiro/diario/exportar.csv')
    assert resposta.status_code == 200
    dados = resposta.get_data(as_text=True)
    assert "'=2+2" in dados and "'@comando" in dados


def test_periodo_invalido_e_lote_inexistente(app, admin_user):
    client = _login(app, admin_user)
    assert client.get('/padeiro/diario/historico?inicio=2026-09-10&fim=2026-09-01').status_code == 400
    assert client.get('/padeiro/diario/lotes/99999').status_code == 404


def test_ordem_antiga_abre_lote_de_hoje_e_formulario_repetido_nao_duplica(app, admin_user, item):
    item.planejamento.data = date(2026, 9, 9)
    db.session.commit()
    client = _login(app, admin_user)
    html = client.get('/padeiro/diario?data=2026-09-09').get_data(as_text=True)
    assert 'name="data_producao"' in html
    assert 'value="2026-09-10"' in html
    chave = str(uuid4())
    lote = _criar(client, item, data_ordem='2026-09-09', chave=chave)
    repetido = _criar(client, item, data_ordem='2026-09-09', chave=chave)
    assert repetido.id == lote.id and ProducaoDiarioLote.query.count() == 1


def test_origem_de_outra_data_ou_nao_enviada_nao_cria_lote(app, admin_user, item):
    client = _login(app, admin_user)
    data = dict(origem=f'item:{item.id}', data_ordem='2026-09-09',
                data_producao='2026-09-10', chave=str(uuid4()))
    assert client.post('/padeiro/diario/lotes', data=data).status_code == 400
    item.planejamento.enviado_ao_padeiro = False
    db.session.commit()
    data['data_ordem'] = DIA.isoformat()
    assert client.post('/padeiro/diario/lotes', data=data).status_code == 400
    assert ProducaoDiarioLote.query.count() == 0

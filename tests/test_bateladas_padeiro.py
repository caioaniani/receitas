"""A ordem nova mostra pesagem por batelada, sem reinterpretar as antigas."""
from datetime import date, timedelta

import pytest

from app.blueprints.padeiro.routes import _plano_do_dia
from app.extensions import db
from app.models import (
    MassaBase,
    MassaBaseItem,
    MateriaPrima,
    MovimentacaoEstoque,
    PlanejamentoItem,
    PlanejamentoItemBatelada,
    PlanejamentoProducao,
    Receita,
    ReceitaEtapa,
    ReceitaIngrediente,
)
from app.services.bateladas_paes import normalizar_item
from app.services.gantt import montar_gantt
from app.services.massa_base import escala_da_ordem
from app.services.producao_diario import origens_do_dia

DIA = date(2026, 9, 16)


@pytest.fixture(autouse=True)
def _dia_fixo(congela_hoje):
    congela_hoje(2026, 9, 16)


def _receita(nome='Sourdough tradicional', lead=0):
    receita = Receita(
        nome=nome, categoria='Pães', peso_base=1000, peso_unitario=600,
        rendimento_qtd=10, rendimento_unidade='un', dias_producao=lead,
        capacidade_amassadeira_g=40000)
    db.session.add(receita)
    db.session.flush()
    for nome_mp, pct in [('Farinha de trigo', 100), ('Água', 70)]:
        if not MateriaPrima.query.filter_by(nome=nome_mp).first():
            db.session.add(MateriaPrima(nome=nome_mp, custo_por_kg=1, estoque_atual=1000000))
        db.session.add(ReceitaIngrediente(
            receita_id=receita.id, tipo='mp', ingrediente_nome=nome_mp,
            porcentagem=pct))
    etapas = [('Amassar', 15, 'amassadeira', True)]
    if lead:
        etapas.append(('Fermentar', 1440, 'camara_fria', False))
    etapas.append(('Assar', 20, 'forno', True))
    for ordem, (nome, duracao, equipamento, ativa) in enumerate(etapas):
        db.session.add(ReceitaEtapa(
            receita_id=receita.id, ordem=ordem, nome=nome,
            duracao_min=duracao, equipamento=equipamento, ativa=ativa))
    db.session.commit()
    return receita


def _item(receita, *, padrao=True, dia=DIA, enviado=True, plano=None):
    if plano is None:
        plano = PlanejamentoProducao(data=dia, origem='cronograma', enviado_ao_padeiro=enviado)
        db.session.add(plano)
        db.session.flush()
    item = PlanejamentoItem(
        planejamento_id=plano.id, receita_id=receita.id, qtd_alvo=71,
        multiplicador=1, produzido_qtd=0)
    db.session.add(item)
    db.session.flush()
    if padrao:
        normalizar_item(item)
    db.session.commit()
    return item


def _base(*receitas):
    base = MassaBase(nome='Base compartilhada antiga')
    db.session.add(base)
    db.session.flush()
    for ordem, receita in enumerate(receitas):
        db.session.add(MassaBaseItem(massa_base_id=base.id, receita_id=receita.id, ordem=ordem))
    db.session.commit()
    return base


def _cliente(app, usuario):
    cliente = app.test_client()
    with cliente.session_transaction() as sess:
        sess['_user_id'] = str(usuario.id)
        sess['_fresh'] = True
    return cliente


def test_snapshot_sai_da_base_sem_mudar_integrante_legado(app):
    nova = _receita()
    antiga = _receita('Sourdough integral')
    novo = _item(nova)
    legado = _item(antiga, padrao=False, plano=novo.planejamento)
    base = _base(nova, antiga)

    painel = _plano_do_dia(DIA)
    assert [item['item_id'] for item in painel['solos']] == [novo.id]
    assert painel['solos'][0]['fornadas'] == 2
    assert painel['solos'][0]['batelada']['farinha_g'] == 25000
    assert [item['item_id'] for item in painel['grupos'][0]['itens']] == [legado.id]
    porcoes, unidades = escala_da_ordem(base, novo.planejamento)
    assert nova.id not in porcoes
    assert unidades == {antiga.id: 71}


def test_ficha_do_item_pesa_batelada_inteira_ignora_quantidade_parcial(app, admin_user):
    item = _item(_receita())
    cliente = _cliente(app, admin_user)
    antes = [mp.estoque_atual for mp in MateriaPrima.query.order_by(MateriaPrima.id)]
    for un in (1, 70, 140):
        resposta = cliente.get(f'/padeiro/receita/{item.receita_id}.json?item_id={item.id}&unidades={un}')
        assert resposta.status_code == 200
        dados = resposta.get_json()
        assert dados['batelada_padrao'] is True
        assert dados['farinha_g'] == 25000
        assert dados['unidades'] == 70
        assert dados['unidades_total'] == 140
        assert dados['bateladas'] == 2
        assert dados['ingredientes'][0]['qtd'] == 25000
        assert dados['avisos']  # 42,5 kg acima dos 40 kg do cadastro de teste.
    assert PlanejamentoItemBatelada.query.count() == 1
    assert MovimentacaoEstoque.query.count() == 0
    assert antes == [mp.estoque_atual for mp in MateriaPrima.query.order_by(MateriaPrima.id)]


def test_ficha_valida_item_e_preserva_legado(app, admin_user):
    receita = _receita()
    legado = _item(receita, padrao=False)
    outro = _item(_receita('Sourdough integral'), dia=DIA + timedelta(days=1), enviado=False)
    cliente = _cliente(app, admin_user)
    assert cliente.get(f'/padeiro/receita/{receita.id}.json?item_id={outro.id}').status_code == 404
    assert cliente.get(f'/padeiro/receita/{outro.receita_id}.json?item_id={outro.id}').status_code == 404
    assert cliente.get(f'/padeiro/receita/{receita.id}.json?item_id=invalido').status_code == 404
    dados = cliente.get(f'/padeiro/receita/{receita.id}.json?item_id={legado.id}&unidades=10').get_json()
    assert not dados.get('batelada_padrao')
    assert dados['unidades'] == 10
    assert legado.batelada_padrao is None


def test_nova_base_nao_aparece_no_modal_nem_diario(app, admin_user):
    item = _item(_receita())
    base = _base(item.receita)
    cliente = _cliente(app, admin_user)
    for dia in (DIA, DIA + timedelta(days=1)):
        dados = cliente.get(f'/padeiro/massa-base/{base.id}.json?data={dia.isoformat()}').get_json()
        assert dados['vazio'] is True
    assert [(o['tipo'], o['id']) for o in origens_do_dia(DIA)] == [('item', item.id)]


def test_gantt_usa_bateladas_e_etapas_congeladas(app, admin_user):
    item = _item(_receita(lead=1))
    _base(item.receita)
    item.receita.etapas[0].duracao_min = 999
    item.receita.etapas[-1].duracao_min = 999
    item.receita.nome = 'Nome alterado depois da ordem'
    item.receita.dias_producao = 2
    db.session.commit()

    hoje = montar_gantt(DIA)['produtos']
    assert len(hoje) == 1
    assert hoje[0]['tipo'] == 'solo'
    assert hoje[0]['nome'] == 'Sourdough tradicional'
    assert hoje[0]['fornadas'] == 2
    assert [t['etapa'] for t in hoje[0]['tarefas']] == ['Amassar']
    assert hoje[0]['destino_etapa'] == 'Fermentar'
    assert hoje[0]['tarefas'][0]['dur'] == 30
    assert hoje[0]['tarefas'][0]['dur_batelada_label'] == '15 min'
    amanha = montar_gantt(DIA + timedelta(days=1))['produtos']
    assert len(amanha) == 1
    assert amanha[0]['tipo'] == 'continuacao'
    assert amanha[0]['fornadas'] == 2
    assert [t['etapa'] for t in amanha[0]['tarefas']] == ['Assar']
    assert amanha[0]['tarefas'][0]['dur'] == 40
    cliente = _cliente(app, admin_user)
    html = cliente.get(f'/padeiro/gantt?data={DIA.isoformat()}').get_data(as_text=True)
    assert '25.000 g de farinha em cada batimento' in html
    assert '15 min por batelada' in html
    assert 'acima da capacidade cadastrada' in html


@pytest.mark.parametrize('nome, farinha, bateladas', [
    ('Sourdough tradicional', '25.000', 2),
    ('Pão francês', '12.000', 3),
])
def test_painel_explicita_pesagem_por_batelada_e_item_no_modal(app, admin_user, nome, farinha, bateladas):
    _item(_receita(nome))
    html = _cliente(app, admin_user).get('/padeiro/').get_data(as_text=True)
    assert f'{farinha} g de farinha em cada batimento' in html
    assert f'{bateladas} bateladas' in html
    assert "'&item_id=' + encodeURIComponent(itemId)" in html
    assert 'Limpar marcações para a próxima batelada' in html

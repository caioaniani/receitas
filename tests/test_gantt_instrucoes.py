"""O passo a passo cadastrado acompanha a agenda sem modificar seus horários."""

from datetime import date, timedelta

from app.extensions import db
from app.models import (
    MassaBase,
    MassaBaseItem,
    PlanejamentoItem,
    PlanejamentoProducao,
    Receita,
    ReceitaEtapa,
    ReceitaIngrediente,
)
from app.services.gantt import montar_gantt

DIA = date(2026, 9, 10)


def _receita(nome, hidratacao=70, lead=0):
    receita = Receita(nome=nome, categoria='Pães', rendimento_qtd=10,
                      rendimento_unidade='un', peso_base=1000,
                      capacidade_amassadeira_g=50000, dias_producao=lead)
    db.session.add(receita)
    db.session.flush()
    for ingrediente, percentual in [('Farinha', 100), ('Água', hidratacao)]:
        db.session.add(ReceitaIngrediente(receita_id=receita.id, tipo='mp',
                                          ingrediente_nome=ingrediente, porcentagem=percentual))
    for ordem, (nome_etapa, duracao, equipamento, ativa, descricao) in enumerate([
        ('Mise en place', 10, None, True, f'Pesar os ingredientes de {nome}.\nConferir a água.'),
        ('Amassamento', 20, 'amassadeira', True, f'Bater {nome} conforme a ficha.'),
        ('Modelagem', 15, None, True, f'Modelar {nome} sem rasgar a superfície.'),
        ('Descanso', 1440 if lead else 30, 'camara_fria' if lead else None,
         False, f'Manter {nome} coberto.'),
        ('Assar', 40, 'forno', True, f'Assar {nome} com vapor.'),
    ]):
        db.session.add(ReceitaEtapa(
            receita_id=receita.id, ordem=ordem, nome=nome_etapa,
            duracao_min=duracao, equipamento=equipamento, ativa=ativa, descricao=descricao))
    db.session.commit()
    return receita


def _plano(receitas, dia=DIA):
    plano = PlanejamentoProducao(data=dia, origem='cronograma', enviado_ao_padeiro=True)
    db.session.add(plano)
    db.session.flush()
    for receita in receitas:
        db.session.add(PlanejamentoItem(planejamento_id=plano.id, receita_id=receita.id,
                                        multiplicador=1, qtd_alvo=10, produzido_qtd=0))
    db.session.commit()


def _grupo():
    primeira = _receita('Tradicional', hidratacao=70)
    segunda = _receita('Integral', hidratacao=80)
    base = MassaBase(nome='Sourdough')
    db.session.add(base)
    db.session.flush()
    for ordem, receita in enumerate([primeira, segunda]):
        db.session.add(MassaBaseItem(massa_base_id=base.id, receita_id=receita.id, ordem=ordem))
    db.session.commit()
    _plano([primeira, segunda])
    return primeira, segunda


def test_solo_preserva_instrucoes_literais_inclusive_descanso_curto(app):
    receita = _receita('Tradicional')
    _plano([receita])
    produto = montar_gantt(DIA)['produtos'][0]
    assert produto['tipo'] == 'solo'
    assert [(t['etapa'], t['descricao']) for t in produto['tarefas']] == [
        (etapa.nome, etapa.descricao) for etapa in receita.etapas]
    assert produto['tarefas'][0]['descricao'].count('\n') == 1
    assert [(t['ini'], t['fim']) for t in produto['tarefas']] == [
        (0, 10), (10, 30), (30, 45), (45, 75), (75, 115)]


def test_destino_longo_carrega_instrucao_sem_inventar_tarefa_do_dia(app):
    receita = _receita('Tradicional', lead=1)
    _plano([receita])
    produto = montar_gantt(DIA)['produtos'][0]
    assert produto['destino_descricao'] == 'Manter Tradicional coberto.'
    assert produto['destino_etapa'] == 'Descanso'
    assert [t['etapa'] for t in produto['tarefas']] == ['Mise en place', 'Amassamento', 'Modelagem']


def test_tronco_usa_instrucao_da_referencia_ramos_usam_suas_fichas(app):
    primeira, segunda = _grupo()
    produtos = montar_gantt(DIA)['produtos']
    tronco = next(prod for prod in produtos if prod['tipo'] == 'base')
    tarefas = {t['etapa']: t for t in tronco['tarefas']}
    assert tarefas['Mise en place']['descricao'] == primeira.etapas[0].descricao
    assert tarefas['Amassar base']['descricao'] == primeira.etapas[1].descricao
    # Passos calculados da cascata não recebem instruções de outra etapa.
    sinteticas = [t for t in tronco['tarefas'] if t['etapa'].startswith(('Tirar ', '+ '))]
    assert sinteticas and all(t['descricao'] == '' for t in sinteticas)
    for receita in (primeira, segunda):
        ramo = next(prod for prod in produtos if prod['receita_id'] == receita.id)
        assert ramo['tipo'] == 'ramo'
        assert [(t['etapa'], t['descricao']) for t in ramo['tarefas']] == [
            (etapa.nome, etapa.descricao) for etapa in receita.etapas[2:]]


def test_continuacao_preserva_instrucao_somente_das_etapas_restantes(app):
    receita = _receita('Tradicional', lead=1)
    _plano([receita], dia=DIA - timedelta(days=1))
    produto = montar_gantt(DIA)['produtos'][0]
    assert produto['tipo'] == 'continuacao'
    assert [(t['etapa'], t['descricao']) for t in produto['tarefas']] == [
        ('Assar', 'Assar Tradicional com vapor.')]
    assert produto['tarefas'][0]['ini'] == 0
    assert produto['tarefas'][0]['dur'] == 40


def test_fallback_solo_da_base_preserva_instrucoes(app, monkeypatch):
    primeira, segunda = _grupo()
    monkeypatch.setattr('app.services.massa_base.calcular_cascata', lambda *args: None)
    produtos = montar_gantt(DIA)['produtos']
    for receita in (primeira, segunda):
        produto = next(prod for prod in produtos if prod['receita_id'] == receita.id)
        assert produto['tipo'] == 'solo'
        assert [t['descricao'] for t in produto['tarefas']] == [e.descricao for e in receita.etapas]


def test_descricao_vazia_nao_cria_instrucao_nem_altera_agendamento(app):
    primeira, segunda = _grupo()

    def agenda(gantt):
        return [(p['nome'], p['tipo'], p['destino'],
                 [{k: v for k, v in t.items() if k != 'descricao'} for t in p['tarefas']])
                for p in gantt['produtos']]

    antes = agenda(montar_gantt(DIA))
    for receita in (primeira, segunda):
        for etapa in receita.etapas:
            etapa.descricao = None
    db.session.commit()
    depois = montar_gantt(DIA)
    assert agenda(depois) == antes
    assert all(t['descricao'] == '' for p in depois['produtos'] for t in p['tarefas'])

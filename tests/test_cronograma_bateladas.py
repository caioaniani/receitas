"""Bateladas por sabor no motor: estoque projetado e BOM não fracionam farinha."""

from datetime import timedelta

import pytest

from app.extensions import db
from app.models import CronogramaOverride, PlanejamentoItem, PlanejamentoProducao, Receita, ReceitaIngrediente
from app.services.cronograma_bateladas import normalizar_cronograma
from app.services.previsao_producao import _explodir_bom, cronograma_producao
from app.utils import hoje


@pytest.fixture(autouse=True)
def segunda(congela_hoje):
    congela_hoje()


def receita(nome='Sourdough Tradicional', *, peso=500, levain=False):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=2,
                rendimento_unidade='un', peso_base=1000, peso_unitario=peso,
                capacidade_amassadeira_g=60000)
    db.session.add(r)
    db.session.flush()
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                      ingrediente_nome='Farinha', porcentagem=100))
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                      ingrediente_nome='Água', porcentagem=60))
    if levain:
        sub = Receita(nome='Levain', categoria='Insumos', peso_base=1000,
                      rendimento_qtd=1, rendimento_unidade='g', peso_unitario=1,
                      sub_na_amassadeira=True, dias_producao=1)
        db.session.add(sub)
        db.session.flush()
        db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='sub_pct',
                                          ingrediente_nome=sub.nome,
                                          sub_receita_id=sub.id, porcentagem=20))
    db.session.commit()
    return r


def linha(r, quantidades):
    return {'receita_id': r.id, 'nome': r.nome, 'total': sum(quantidades),
            'por_dia': [{'data': (hoje() + timedelta(days=i)).isoformat(),
                         'qtd': q, 'fornadas': None}
                        for i, q in enumerate(quantidades)]}


def normalizar(rs, rows, *, piso=0, overrides=None, fixos=None, snapshots=None):
    dias = [hoje() + timedelta(days=i) for i in range(len(rows[0]['por_dia']))]
    recs = {r.id: r for r in rs}
    normalizar_cronograma(rows, dias, recs, {}, piso=piso,
                          contexto=(overrides or {}, fixos or {}, snapshots or {}))
    return [c['qtd'] for c in rows[0]['por_dia']]


def test_sobra_cobre_dias_seguintes_sem_repetir_batelada(app):
    r = receita()  # 25kg farinha + 15kg água = 80 pães.
    rows = [linha(r, [30, 30, 30, 30])]
    assert normalizar([r], rows) == [80, 0, 80, 0]
    assert rows[0]['total'] == 160
    assert rows[0]['por_dia'][0]['farinha_total_g'] == 25000


def test_piso_de_200_completa_por_lotes_nao_por_parcelas(app):
    rs = [receita(f'Sourdough {nome}') for nome in ('Tradicional', 'Grãos', 'Nozes')]
    rows = [linha(r, [0, 0, 0, 0, 0, 0, 0]) for r in rs]
    normalizar(rs, rows, piso=200)
    for i in range(5):
        assert sum(rr['por_dia'][i]['qtd'] for rr in rows) == 240
        assert all(rr['por_dia'][i]['qtd'] % 80 == 0 for rr in rows)
    for i in (5, 6):
        assert sum(rr['por_dia'][i]['qtd'] for rr in rows) == 0
    assert [rr['total'] for rr in rows] == [400, 400, 400]


def test_frances_usa_12kg_e_brioche_nao_muda(app):
    frances, brioche = receita('Pão Francês'), receita('Brioche')
    rows = [linha(frances, [1]), linha(brioche, [37])]
    normalizar([frances, brioche], rows)
    assert rows[0]['por_dia'][0]['qtd'] == 38
    assert rows[0]['por_dia'][0]['farinha_total_g'] == 12000
    assert rows[1]['por_dia'][0]['qtd'] == 37
    assert 'batelada_padrao' not in rows[1]['por_dia'][0]


def test_override_zero_nao_recebe_piso_e_positivo_arredonda(app):
    r = receita()
    rows = [linha(r, [0, 85])]
    normalizar([r], rows, piso=200,
               overrides={(r.id, hoje()): 0,
                          (r.id, hoje() + timedelta(days=1)): 85})
    assert [c['qtd'] for c in rows[0]['por_dia']] == [0, 160]


def test_teto_impossivel_nao_cria_meio_lote(app):
    r = receita()
    r.producao_max_dia = 40
    rows = [linha(r, [20])]
    normalizar([r], rows, piso=200)
    assert rows[0]['por_dia'][0]['qtd'] == 0
    assert rows[0]['por_dia'][0]['batelada_bloqueada']


def test_congelado_nao_inventa_excedente_para_amanha(app):
    r = receita()
    rows = [linha(r, [30, 50])]
    normalizar([r], rows, fixos={(r.id, hoje()): 30})
    assert [c['qtd'] for c in rows[0]['por_dia']] == [30, 80]
    assert rows[0]['por_dia'][0]['batelada_congelada']
    assert 'batelada_padrao' not in rows[0]['por_dia'][0]


def test_ordem_congelada_zerada_consume_sobra_de_ontem(app):
    r = receita()
    rows = [linha(r, [30, 40, 30])]
    normalizar([r], rows, fixos={(r.id, hoje() + timedelta(days=1)): 0})
    assert [c['qtd'] for c in rows[0]['por_dia']] == [80, 0, 80]


def test_bom_usa_farinha_inteira_mesmo_rendimento_fracionario(app):
    r = receita(peso=537, levain=True)  # 45kg / 537g = 83,79; lote = 83.
    sub = Receita.query.filter_by(nome='Levain').one()
    rows = [linha(r, [0, 1])]
    normalizar([r, sub], rows)
    assert rows[0]['por_dia'][1]['qtd'] == 83
    dias = [hoje(), hoje() + timedelta(days=1)]
    _explodir_bom(rows, dias, {r.id: r, sub.id: sub}, {r.id: 0, sub.id: 1},
                  {'itens': []})
    levain = next(rr for rr in rows if rr['receita_id'] == sub.id)
    assert levain['por_dia'][0]['qtd'] == 5000
    assert levain['consumo_janela'] == 5000


def test_snapshot_congela_bom_apos_editar_ficha(app):
    from app.services.bateladas_paes import padrao_receita
    r = receita(peso=537, levain=True)
    sub = Receita.query.filter_by(nome='Levain').one()
    dados = padrao_receita(r, resolver_estoque=False)
    ing = next(i for i in r.ingredientes if i.sub_receita_id)
    ing.porcentagem = 50
    db.session.commit()
    rows = [linha(r, [0, 83])]
    dia = hoje() + timedelta(days=1)
    normalizar([r, sub], rows, snapshots={(r.id, dia): dados},
               fixos={(r.id, dia): 83})
    _explodir_bom(rows, [hoje(), dia], {r.id: r, sub.id: sub},
                  {r.id: 0, sub.id: 1}, {'itens': []})
    levain = next(rr for rr in rows if rr['receita_id'] == sub.id)
    assert levain['consumo_janela'] == 5000


def test_motor_inteiro_piso_com_brioche_e_override(app):
    app.config['SOURDOUGH_MIN_DIA'] = 200
    r = receita()
    brioche = receita('Brioche')
    db.session.add(CronogramaOverride(receita_id=brioche.id, data=hoje(), qtd=40))
    db.session.commit()
    crono = cronograma_producao(horizonte_dias=3, equilibrar=True)
    row = next(rr for rr in crono['receitas'] if rr['receita_id'] == r.id)
    assert [c['qtd'] for c in row['por_dia']] == [240, 240, 240]
    assert next(rr for rr in crono['receitas'] if rr['receita_id'] == brioche.id)['total'] == 40


def test_motor_preserva_plano_enviado_hoje_sem_escrita(app):
    app.config['SOURDOUGH_MIN_DIA'] = 200
    r = receita()
    plano = PlanejamentoProducao(data=hoje(), nome='Ordem', origem='cronograma',
                                 enviado_ao_padeiro=True)
    db.session.add(plano)
    db.session.flush()
    item = PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                            multiplicador=1, qtd_alvo=30)
    db.session.add(item)
    db.session.commit()
    crono = cronograma_producao(horizonte_dias=2)
    rr = next(rr for rr in crono['receitas'] if rr['receita_id'] == r.id)
    assert rr['por_dia'][0]['qtd'] == 30
    assert rr['por_dia'][1]['qtd'] == 240
    assert item.qtd_alvo == 30
    assert item.batelada_padrao is None


def test_override_humano_em_congelado_nao_credita_sobra_hipotetica(app):
    r = receita()
    rows = [linha(r, [85, 10])]
    normalizar([r], rows, overrides={(r.id, hoje()): 85},
               fixos={(r.id, hoje()): 30})
    assert [c['qtd'] for c in rows[0]['por_dia']] == [85, 80]
    assert rows[0]['por_dia'][0]['batelada_override_pendente']


def test_motor_freeze_insumo_preserva_amanha_e_nao_cria_sobra(app):
    app.config['SOURDOUGH_MIN_DIA'] = 200
    r = receita(levain=True)
    amanha = hoje() + timedelta(days=1)
    plano = PlanejamentoProducao(data=amanha, nome='Ordem', origem='cronograma',
                                 enviado_ao_padeiro=True)
    db.session.add(plano)
    db.session.flush()
    db.session.add(PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                                    multiplicador=1, qtd_alvo=30))
    db.session.commit()
    crono = cronograma_producao(horizonte_dias=3)
    rr = next(rr for rr in crono['receitas'] if rr['receita_id'] == r.id)
    assert rr['por_dia'][1]['qtd'] == 30
    assert rr['por_dia'][1]['batelada_congelada']
    assert rr['por_dia'][2]['qtd'] == 270


def test_pao_como_insumo_preserva_lote_e_sobra(app):
    from app.services.cronograma_bateladas import adicionar_demanda_insumo
    r = receita()
    rows = [linha(r, [30, 30])]
    normalizar([r], rows)
    novo = adicionar_demanda_insumo(rows[0], [10, 10], r)
    assert novo == [80, 0]  # A mesma sobra cobre venda e ingrediente.
    assert all(c['qtd'] % 80 == 0 for c in rows[0]['por_dia'])


def test_mrp_pao_somente_como_ingrediente_tambem_em_batelada(app):
    r = receita()
    pai = Receita(nome='Sanduíche', categoria='Montagem', peso_base=1000,
                  rendimento_qtd=1, rendimento_unidade='un')
    db.session.add(pai)
    db.session.flush()
    db.session.add(ReceitaIngrediente(receita_id=pai.id, tipo='receita',
                                      ingrediente_nome=r.nome,
                                      sub_receita_id=r.id, porcentagem=1))
    db.session.commit()
    rows = [linha(pai, [1])]
    _explodir_bom(rows, [hoje()], {r.id: r, pai.id: pai}, {}, {'itens': []})
    pao = next(rr for rr in rows if rr['receita_id'] == r.id)
    assert pao['por_dia'][0]['qtd'] == 80
    assert pao['por_dia'][0]['farinha_total_g'] == 25000


def test_ficha_invalida_bloqueia_com_motivo_explicito(app):
    r = receita()
    r.ingredientes.clear()
    db.session.commit()
    rows = [linha(r, [20])]
    normalizar([r], rows, piso=200)
    assert rows[0]['total'] == 0
    assert 'cadastre a farinha' in rows[0]['erro_batelada']
    assert rows[0]['por_dia'][0]['batelada_bloqueada']


@pytest.mark.parametrize('alteracao', ['ingredientes', 'classificacao'])
def test_snapshot_futuro_prevalece_sem_freeze_com_ficha_editada(app, alteracao):
    from app.services.bateladas_paes import padrao_receita
    r = receita()
    dados = padrao_receita(r, resolver_estoque=False)
    if alteracao == 'ingredientes':
        r.ingredientes.clear()
    else:
        r.nome = 'Novo nome sem classificação'
        r.familia = None
    db.session.commit()
    rows = [linha(r, [0, 1])]
    normalizar([r], rows, snapshots={(r.id, hoje() + timedelta(days=1)): dados})
    c = rows[0]['por_dia'][1]
    assert c['qtd'] == 80
    assert c['farinha_total_g'] == 25000
    assert 'batelada_bloqueada' not in c


@pytest.mark.parametrize('enviado', [False, True])
def test_contexto_encontra_snapshot_mesmo_receita_reclassificada(app, enviado):
    from app.models.producao_batelada import PlanejamentoItemBatelada
    from app.services.bateladas_paes import padrao_receita
    from app.services.cronograma_bateladas import _contexto_ordens
    r = receita()
    dados = padrao_receita(r, resolver_estoque=False)
    futuro = hoje() + timedelta(days=4)
    plano = PlanejamentoProducao(data=futuro, nome='Ordem', origem='cronograma',
                                 enviado_ao_padeiro=enviado)
    item = PlanejamentoItem(planejamento=plano, receita=r,
                            multiplicador=1, qtd_alvo=80)
    db.session.add(PlanejamentoItemBatelada(item=item, dados=dados, bateladas=1))
    r.nome, r.familia = 'Nome novo', None
    db.session.commit()
    _overrides, fixos, snapshots = _contexto_ordens([futuro], {r.id: r}, {})
    assert (r.id, futuro) not in fixos
    assert snapshots[r.id, futuro]['farinha_g'] == 25000


@pytest.mark.parametrize('produzido', [40, 80])
def test_confirmado_no_fisico_nao_duplica_projecao_nem_esconde_falta(app, produzido):
    from app.models import EstoqueProducao, Loja, PedidoItem, PedidoLoja
    r = receita()
    loja = Loja(nome='Loja Teste', ativa=True)
    db.session.add(loja)
    db.session.flush()
    pedido = PedidoLoja(loja_id=loja.id, status='pendente',
                        data_pedido=hoje(), data_entrega=hoje())
    pedido.itens.append(PedidoItem(receita_id=r.id, quantidade=120))
    plano = PlanejamentoProducao(data=hoje(), nome='Ordem', origem='cronograma',
                                 enviado_ao_padeiro=True)
    plano.itens.append(PlanejamentoItem(receita_id=r.id, multiplicador=1,
                                       qtd_alvo=80, produzido_qtd=produzido))
    db.session.add_all([pedido, plano, EstoqueProducao(receita_id=r.id,
                                                     quantidade=produzido)])
    db.session.commit()
    crono = cronograma_producao(horizonte_dias=1)
    rr = next(rr for rr in crono['receitas'] if rr['receita_id'] == r.id)
    assert rr['por_dia'][0]['qtd'] == 80
    assert rr['por_dia'][0]['qtd_pendente'] == 80 - produzido
    assert rr['projecao'][0]['saldo'] == -40
    assert rr['entregas_risco'][0]['faltam'] == 40


@pytest.mark.parametrize(('produzido', 'consumo'), [(45, 2500), (90, 0)])
def test_mrp_nao_consumir_levain_ja_confirmado(app, produzido, consumo):
    from app.services.bateladas_paes import padrao_receita
    r = receita(levain=True)
    sub = Receita.query.filter_by(nome='Levain').one()
    dados = padrao_receita(r, resolver_estoque=False)
    rows = [linha(r, [90])]
    normalizar([r, sub], rows,
               snapshots={(r.id, hoje()): dados},
               fixos={(r.id, hoje()): {'alvo': 90, 'produzido': produzido}})
    _explodir_bom(rows, [hoje()], {r.id: r, sub.id: sub}, {}, {'itens': []})
    linha_sub = next((rr for rr in rows if rr['receita_id'] == sub.id), None)
    assert (linha_sub['consumo_janela'] if linha_sub else 0) == consumo


def test_proposta_manual_congelada_nao_vira_producao_fantasma(app):
    from app.services.cronograma_bateladas import quantidade_pendente
    r = receita()
    rows = [linha(r, [300])]
    normalizar([r], rows, overrides={(r.id, hoje()): 300},
               fixos={(r.id, hoje()): {'alvo': 80, 'produzido': 40}})
    c = rows[0]['por_dia'][0]
    assert c['qtd'] == 300  # Intenção humana continua visível.
    assert quantidade_pendente(c) == 40  # Apenas a ordem real cobre pedidos.


def test_snapshot_preserva_janela_do_insumo_removido_da_ficha_viva(app):
    from app.models.producao_batelada import PlanejamentoItemBatelada
    from app.services.bateladas_paes import padrao_receita
    from app.services.cronograma_bateladas import _contexto_ordens
    r = receita(levain=True)
    dados = {**padrao_receita(r, resolver_estoque=False), 'antecedencia_insumo_dias': 1}
    amanha = hoje() + timedelta(days=1)
    plano = PlanejamentoProducao(data=amanha, nome='Ordem', origem='cronograma',
                                 enviado_ao_padeiro=True)
    item = PlanejamentoItem(planejamento=plano, receita=r, multiplicador=1, qtd_alvo=90)
    db.session.add(PlanejamentoItemBatelada(item=item, dados=dados, bateladas=1))
    r.ingredientes = [i for i in r.ingredientes if not i.sub_receita_id]
    db.session.commit()
    _overrides, fixos, _snapshots = _contexto_ordens([amanha], {r.id: r}, {})
    assert fixos[r.id, amanha]['alvo'] == 90


def test_snapshot_encerrado_parcial_nao_reabre_por_arredondamento(app):
    from app.services.bateladas_paes import padrao_receita
    r = receita()
    dados = padrao_receita(r, resolver_estoque=False)
    rows = [linha(r, [40])]
    normalizar([r], rows, snapshots={(r.id, hoje()): dados},
               fixos={(r.id, hoje()): {'alvo': 40, 'produzido': 40}})
    c = rows[0]['por_dia'][0]
    assert c['qtd'] == 40
    assert c['qtd_pendente'] == 0
    assert c['bateladas'] == 1


@pytest.mark.parametrize('manual', [False, True])
def test_snapshot_parcial_aberto_sem_nova_demanda_nao_inventa_lote(app, manual):
    from app.services.bateladas_paes import padrao_receita
    r = receita()
    dados = {**padrao_receita(r, resolver_estoque=False), 'produzido_confirmado': 40}
    rows = [linha(r, [0, 30])]
    normalizar([r], rows, snapshots={(r.id, hoje()): dados},
               overrides={(r.id, hoje()): 0} if manual else {})
    c = rows[0]['por_dia'][0]
    assert c['qtd'] == 40
    assert c['qtd_pendente'] == 0
    assert c['estoque_virtual_batelada'] == 0
    assert rows[0]['por_dia'][1]['qtd'] == 80

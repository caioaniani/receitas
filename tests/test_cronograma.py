"""Cronograma de producao POR DIA (previsao_producao.cronograma_producao) +
rota /telaindustriateste.

Distribui a producao por dia acompanhando as entregas (deslocado pelo lead),
descontando o estoque dos primeiros dias.
"""
from datetime import timedelta

from app.extensions import db
from app.models import EstoqueProducao, Loja, PedidoItem, PedidoLoja, Receita
from app.services.previsao_producao import cronograma_producao
from app.utils import hoje


def _receita(nome='Pão'):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0)
    db.session.add(r)
    db.session.commit()
    return r


def _loja(nome='Loja A'):
    loja = Loja(nome=nome, ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


def _pedido(loja, status, data_entrega, receita, qtd):
    p = PedidoLoja(loja_id=loja.id, status=status, data_entrega=data_entrega,
                   data_pedido=data_entrega)
    db.session.add(p)
    db.session.flush()
    db.session.add(PedidoItem(pedido_id=p.id, receita_id=receita.id,
                              quantidade=qtd))
    db.session.commit()
    return p


def _rec_out(crono, rid):
    return next((x for x in crono['receitas'] if x['receita_id'] == rid), None)


def test_distribui_firme_por_dia(app):
    """Pedido firme cai no dia de producao = dia de entrega (lead 0)."""
    loja = _loja()
    r = _receita()
    _pedido(loja, 'pendente', hoje() + timedelta(days=2), r, 50)

    crono = cronograma_producao(horizonte_dias=7, janela_semanas=6)
    rr = _rec_out(crono, r.id)
    assert rr is not None
    assert rr['por_dia'][2]['qtd'] == 50    # entrega hoje+2, lead 0
    assert rr['por_dia'][0]['qtd'] == 0     # nada hoje
    assert rr['total'] == 50


def test_lead_antecipa_producao(app):
    """Com lead 2, a producao de uma entrega em hoje+2 cai HOJE."""
    loja = _loja()
    r = _receita()
    r.dias_producao = 2
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=2), r, 30)

    crono = cronograma_producao(horizonte_dias=7)
    rr = _rec_out(crono, r.id)
    assert rr['por_dia'][0]['qtd'] == 30    # produz hoje p/ entregar em hoje+2


def test_estoque_cobre_primeiros_dias(app):
    """Estoque pronto desconta dos dias mais proximos primeiro."""
    loja = _loja()
    r = _receita()
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=20))
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 30)
    _pedido(loja, 'pendente', hoje() + timedelta(days=3), r, 30)

    crono = cronograma_producao(horizonte_dias=7)
    rr = _rec_out(crono, r.id)
    assert rr['por_dia'][1]['qtd'] == 10    # 30 - 20 de estoque
    assert rr['por_dia'][3]['qtd'] == 30
    assert rr['total'] == 40


def test_lead_com_estoque_unifica_com_balanco(app):
    """Unificado E correto: o cronograma distribui o "Produzir" do BALANÇO, e o
    estoque é alocado cronologicamente. Com lead 2 e estoque 40: a entrega de
    hoje+1 (iminente, 30) consome 30 do estoque -> sobram 10 efetivos pra a
    janela (hoje+2 + hoje+4 = 60), logo produzir = 50. O estoque NÃO é contado
    duas vezes (bug que subproduziria pra 20)."""
    from app.services.previsao_producao import balanco_industria
    loja = _loja()
    r = _receita()
    r.dias_producao = 2
    db.session.commit()
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=40))
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 30)
    _pedido(loja, 'pendente', hoje() + timedelta(days=2), r, 30)
    _pedido(loja, 'pendente', hoje() + timedelta(days=4), r, 30)

    crono = cronograma_producao(horizonte_dias=7)
    rr = _rec_out(crono, r.id)
    bal = balanco_industria(horizonte_dias=7, inicio_offset_dias=0,
                            usar_cache=False)
    bit = next(i for i in bal['itens'] if i['receita_id'] == r.id)
    assert bit['em_estoque_efetivo'] == 10       # 40 - 30 da entrega iminente
    assert bit['produzir'] == 50                 # 60 - 10 efetivo (NÃO 20)
    assert rr['total'] == bit['produzir']         # a unificação
    # hoje+2: falta 20 -> produz dia 0 (hoje); hoje+4: 30 -> dia 2 (hoje+4-lead2)
    assert rr['por_dia'][0]['qtd'] == 20
    assert rr['por_dia'][2]['qtd'] == 30


def test_cronograma_total_bate_com_balanco(app):
    """Invariante da unificação: para cada receita, o total do cronograma é
    EXATAMENTE o 'Produzir' do balanço (mesmas receitas, mesmos totais), em
    qualquer início — e a soma das células de cada receita == o total."""
    from app.services.previsao_producao import balanco_industria
    loja_a = _loja('A')
    loja_b = _loja('B')
    r1 = _receita('Pão')
    r2 = _receita('Croissant')
    r1.dias_producao = 1
    r2.dias_producao = 1
    db.session.add(EstoqueProducao(receita_id=r1.id, quantidade=15))
    db.session.commit()
    hoje_d = hoje()
    for d in (2, 3, 5):
        _pedido(loja_a, 'pendente', hoje_d + timedelta(days=d), r1, 20)
        _pedido(loja_b, 'pendente', hoje_d + timedelta(days=d), r2, 12)

    for off in (0, 1, 2):
        crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=off)
        bal = balanco_industria(horizonte_dias=7, inicio_offset_dias=off,
                                usar_cache=False)
        prod = {i['receita_id']: i['produzir']
                for i in bal['itens'] if i['produzir'] > 0}
        crono_tot = {x['receita_id']: x['total'] for x in crono['receitas']}
        assert crono_tot == prod, f'offset {off}: {crono_tot} != {prod}'
        for x in crono['receitas']:
            assert sum(c['qtd'] for c in x['por_dia']) == x['total']


def _receita_amassadeira(nome, rend, peso_base, cap):
    """Receita que passa pela amassadeira: rend un/receita, massa = peso_base
    (1 ingrediente mp a 100%), capacidade cap g. unidades/fornada = cap*rend/
    massa_base = cap*rend/peso_base."""
    from app.models import ReceitaIngrediente
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=rend,
                rendimento_unidade='un', peso_base=float(peso_base),
                capacidade_amassadeira_g=cap)
    db.session.add(r)
    db.session.flush()
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                      ingrediente_nome='Farinha', porcentagem=100))
    db.session.commit()
    return r


def test_dribble_consolida_no_dia_anterior(app):
    """C1: um dribble (< fração de UMA fornada) consolida PRA TRÁS, no dia de um
    lote real ANTERIOR — produzido a tempo, nunca empurrado pra frente (que
    atrasaria a entrega). Total preservado."""
    loja = _loja()
    # cap=5000, rend=50, massa_base=peso_base=5000 -> unid/fornada=50, minimo=10.
    r = _receita_amassadeira('Sourdough', rend=50, peso_base=5000, cap=5000)
    # 60 pra hoje+1 (lote real) + 1 pra hoje+3 (dribble, entrega POSTERIOR).
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 60)
    _pedido(loja, 'pendente', hoje() + timedelta(days=3), r, 1)

    crono = cronograma_producao(horizonte_dias=7, janela_semanas=6)
    rr = _rec_out(crono, r.id)
    assert rr['total'] == 61                        # balanco: 61 a produzir
    # o "1" do dia 3 puxou PRA TRAS, somando no lote do dia 1 (pronto a tempo).
    assert rr['por_dia'][1]['qtd'] == 61
    assert rr['por_dia'][3]['qtd'] == 0
    minimo = 10                                     # 20% de 50 un/fornada
    for c in rr['por_dia']:
        assert not (0 < c['qtd'] < minimo), f"dribble nao consolidado: {c}"
    assert sum(c['qtd'] for c in rr['por_dia']) == rr['total']


def test_dribble_de_entrega_iminente_nao_e_empurrado(app):
    """C1: um dribble pra entrega IMINENTE (hoje) NÃO é empurrado pra um lote
    posterior — isso entregaria dias tarde. Sem dia anterior pra consolidar, ele
    é produzido HOJE mesmo (batida pequena) pra cumprir o prazo."""
    loja = _loja()
    r = _receita_amassadeira('Sourdough', rend=50, peso_base=5000, cap=5000)
    # 1 un pra entregar HOJE (dribble) + 60 pra hoje+3 (lote posterior).
    _pedido(loja, 'pendente', hoje(), r, 1)
    _pedido(loja, 'pendente', hoje() + timedelta(days=3), r, 60)

    crono = cronograma_producao(horizonte_dias=7, janela_semanas=6)
    rr = _rec_out(crono, r.id)
    assert rr['total'] == 61
    # o "1" de HOJE fica no dia 0 (produz no prazo), NAO rola pro dia 3.
    assert rr['por_dia'][0]['qtd'] == 1
    assert rr['por_dia'][3]['qtd'] == 60
    assert sum(c['qtd'] for c in rr['por_dia']) == rr['total']


def test_sem_amassadeira_nao_consolida(app):
    """Item que NAO passa pela amassadeira (cap=0: Moeda, creme) nao tem
    'fornada' a desperdicar — produzir 1 num dia continua valendo, nao rola."""
    loja = _loja()
    r = Receita(nome='Moeda', categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=10.0,
                capacidade_amassadeira_g=0)
    db.session.add(r)
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 1)

    crono = cronograma_producao(horizonte_dias=7, janela_semanas=6)
    rr = _rec_out(crono, r.id)
    assert rr['por_dia'][1]['qtd'] == 1            # produz o 1 no dia, sem rolar
    assert rr['total'] == 1


def test_equilibrar_enche_dia_ocioso(app):
    """Modo equilibrar: cada receita INTEIRA num dia; adianta receitas pra
    encher dia vazio (nivela fornadas). cap pequeno -> nao dispara a
    consolidacao anti-dribble (minimo=1)."""
    loja = _loja()
    r1 = _receita_amassadeira('Pao A', rend=50, peso_base=5000, cap=500)
    r2 = _receita_amassadeira('Pao B', rend=50, peso_base=5000, cap=500)
    d1 = hoje() + timedelta(days=1)
    _pedido(loja, 'pendente', d1, r1, 30)
    _pedido(loja, 'pendente', d1, r2, 30)

    # SEM equilibrar: ambas caem no dia 1 (entrega); dia 0 ocioso.
    base = cronograma_producao(horizonte_dias=2, inicio_offset_dias=0)
    a = _rec_out(base, r1.id)
    b = _rec_out(base, r2.id)
    assert a['por_dia'][0]['qtd'] == 0 and b['por_dia'][0]['qtd'] == 0
    assert a['por_dia'][1]['qtd'] == 30 and b['por_dia'][1]['qtd'] == 30

    # COM equilibrar: cada receita inteira num unico dia; dia 0 nao fica ocioso.
    eq = cronograma_producao(horizonte_dias=2, inicio_offset_dias=0,
                             equilibrar=True)
    ae = _rec_out(eq, r1.id)
    be = _rec_out(eq, r2.id)
    for rr in (ae, be):
        dias_com = [c for c in rr['por_dia'] if c['qtd'] > 0]
        assert len(dias_com) == 1, 'receita nao pode ser dividida'
        assert dias_com[0]['qtd'] == 30
        assert sum(c['qtd'] for c in rr['por_dia']) == rr['total'] == 30
    assert (ae['por_dia'][0]['qtd'] > 0) or (be['por_dia'][0]['qtd'] > 0), \
        'equilibrar deveria adiantar uma receita pro dia ocioso'
    assert {ae['por_dia'][0]['qtd'], be['por_dia'][0]['qtd']} == {0, 30}


def test_cronograma_padroniza_em_lotes(app):
    """Produção sai em LOTES inteiros quando a receita tem lote_pedido (não
    produzir picado): cada dia é múltiplo do lote (ou 0) e o total também."""
    loja = _loja()
    r = _receita('Pão Francês')
    r.lote_pedido = 50
    db.session.commit()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 137)   # demanda que daria nº quebrado
    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr = _rec_out(crono, r.id)
    assert rr is not None
    assert rr['total'] > 0 and rr['total'] % 50 == 0
    for c in rr['por_dia']:
        assert c['qtd'] % 50 == 0


def test_fornada_especial_produz_na_vespera(app):
    """Fornada especial (vende sex/sáb/dom) com lead 1: produzida na QUINTA pra a
    venda na SEXTA. A regra restringe a VENDA; o lead desloca a produção pra
    véspera — então a quinta NÃO é bloqueada."""
    loja = _loja()
    r = _receita('Focaccia')
    r.fornada_especial = True
    r.dias_producao = 1
    db.session.commit()
    hoje_d = hoje()
    sexta = next(hoje_d + timedelta(days=i) for i in range(1, 14)
                 if (hoje_d + timedelta(days=i)).weekday() == 4)
    quinta = sexta - timedelta(days=1)
    _pedido(loja, 'pendente', sexta, r, 50)           # entrega na sexta
    horizonte = (sexta - hoje_d).days + 1
    crono = cronograma_producao(horizonte_dias=horizonte, inicio_offset_dias=0)
    rr = _rec_out(crono, r.id)
    assert rr is not None
    por_data = {c['data']: c['qtd'] for c in rr['por_dia']}
    assert por_data.get(quinta.isoformat(), 0) > 0    # produz na quinta
    assert por_data.get(sexta.isoformat(), 0) == 0    # não na sexta


def test_bom_explode_sub_receita(app):
    """MRP: pedir croissant gera produção da MASSA PARA FOLHAR (sub-receita não
    vendida), produzida ANTES do croissant (lead), na quantidade certa."""
    from app.models import ReceitaIngrediente
    loja = _loja()
    massa = _receita('Massa para folhar')        # rend 1, não vendida
    massa.dias_producao = 1
    cro = _receita('Croissant Tradicional')
    cro.rendimento_qtd = 50
    db.session.add(ReceitaIngrediente(
        receita_id=cro.id, tipo='receita', sub_receita_id=massa.id,
        ingrediente_nome='Massa para folhar', porcentagem=1))
    db.session.commit()
    d3 = hoje() + timedelta(days=3)
    _pedido(loja, 'pendente', d3, cro, 100)

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rc = _rec_out(crono, cro.id)
    rm = _rec_out(crono, massa.id)
    assert rc is not None and rc['total'] == 100
    assert rm is not None
    assert rm['total'] == 2                       # 100 × (1/50) = 2 un de massa
    dia_cro = next(i for i, c in enumerate(rc['por_dia']) if c['qtd'] > 0)
    dia_massa = next(i for i, c in enumerate(rm['por_dia']) if c['qtd'] > 0)
    assert dia_massa < dia_cro                     # massa produzida antes


def test_bom_explode_cadeia_multinivel(app):
    """Cadeia: Croissant Almond consome Croissant Tradicional (vendido E insumo)
    + Creme de Amêndoas; o tradicional consome Massa para folhar. Pedir almond
    explode os 3 níveis e SOMA o tradicional vendido + o consumido pelo almond."""
    from app.models import ReceitaIngrediente
    loja = _loja()
    massa = _receita('Massa para folhar')
    creme = _receita('Creme de Amêndoas')
    trad = _receita('Croissant Tradicional')
    trad.rendimento_qtd = 50
    almond = _receita('Croissant Almond')
    almond.rendimento_qtd = 50
    db.session.add_all([
        # 1 croissant consome 1/50 un de massa (1 batida p/ 50)
        ReceitaIngrediente(receita_id=trad.id, tipo='receita',
                           sub_receita_id=massa.id, ingrediente_nome='Massa',
                           porcentagem=1),
        # 1 almond consome 1 tradicional e 1 creme (porcentagem=rend -> 1:1)
        ReceitaIngrediente(receita_id=almond.id, tipo='receita',
                           sub_receita_id=trad.id, ingrediente_nome='Trad',
                           porcentagem=50),
        ReceitaIngrediente(receita_id=almond.id, tipo='receita',
                           sub_receita_id=creme.id, ingrediente_nome='Creme',
                           porcentagem=50),
    ])
    db.session.commit()
    d3 = hoje() + timedelta(days=3)
    _pedido(loja, 'pendente', d3, trad, 50)        # 50 tradicionais vendidos
    _pedido(loja, 'pendente', d3, almond, 50)      # 50 almonds

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rt = _rec_out(crono, trad.id)
    rcr = _rec_out(crono, creme.id)
    rm = _rec_out(crono, massa.id)
    # tradicional = 50 vendidos + 50 pro almond = 100
    assert rt['total'] == 100
    assert rcr['total'] == 50                       # 50 almond × 1
    assert rm['total'] == 2                         # 100 trad × 1/50


def test_bom_insumo_fracionario_nao_infla_por_dia(app):
    """D1: insumo de fração baixa não infla por dar ceil em CADA dia. Croissant
    (rend 5 -> 0,2 massa/un) produzido 2/dia em 4 dias = 0,4 massa/dia. A massa
    total é ceil(4 × 0,4) = ceil(1,6) = 2, NÃO a soma dos ceils por dia
    (ceil(0,4) × 4 = 4). A fração acumula entre os dias."""
    from app.models import ReceitaIngrediente
    loja = _loja()
    massa = _receita('Massa para folhar')          # lead 0, fica no mesmo dia
    cro = _receita('Croissant')
    cro.rendimento_qtd = 5                          # 1/5 = 0,2 massa por croissant
    db.session.add(ReceitaIngrediente(
        receita_id=cro.id, tipo='receita', sub_receita_id=massa.id,
        ingrediente_nome='Massa para folhar', porcentagem=1))
    db.session.commit()
    for dia in (1, 2, 3, 4):                        # 2 croissants em 4 dias distintos
        _pedido(loja, 'pendente', hoje() + timedelta(days=dia), cro, 2)

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rc = _rec_out(crono, cro.id)
    rm = _rec_out(crono, massa.id)
    assert rc is not None and rc['total'] == 8      # 2/dia × 4 dias
    assert rm is not None
    assert rm['total'] == 2                          # ceil(1,6), não 4 (soma de ceils)


def test_cronograma_ordena_por_categoria(app):
    """Cronograma agrupa as receitas por categoria (não espalhado por demanda)."""
    loja = _loja()
    r1 = _receita('Zebra'); r1.categoria = 'Aves'
    r2 = _receita('Abacaxi'); r2.categoria = 'Zoo'
    r3 = _receita('Melao'); r3.categoria = 'Aves'
    db.session.commit()
    d2 = hoje() + timedelta(days=2)
    for r in (r1, r2, r3):
        _pedido(loja, 'pendente', d2, r, 30)

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    cats = [rr['categoria'] for rr in crono['receitas']]
    assert cats == sorted(cats)                    # categorias agrupadas
    nomes_aves = [rr['nome'] for rr in crono['receitas']
                  if rr['categoria'] == 'Aves']
    assert nomes_aves == sorted(nomes_aves)        # dentro da categoria, por nome


def test_cronograma_expoe_saldo(app):
    """Cronograma expõe estoque, pedido programado (comprometido) e saldo por
    receita — pro expandir da tela (estoque − pedido = saldo)."""
    loja = _loja('Loja A')
    r = _receita('Pão')
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=10))
    db.session.commit()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 30)            # produzir = 30-10 = 20 > 0

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr = _rec_out(crono, r.id)
    assert rr is not None
    assert rr['em_estoque'] == 10
    assert rr['comprometido'] == 30                 # pedido programado
    assert rr['saldo'] == -20                       # 10 - 30
    assert any(b['loja_nome'] == 'Loja A' and b['qtd'] == 30
               for b in rr['breakdown'])


def test_produto_vendavel_sem_demanda_aparece(app):
    """Produto que a loja PEDE (sugerir_pedido_loja != False) aparece na grade
    mesmo sem pedido/estoque na janela — pro planejamento (ex: Pain au
    Chocolat). O balanço o exclui (zerado); o cronograma o injeta zerado."""
    _loja()
    pain = _receita('Pain au Chocolat')             # sem pedido, sem estoque
    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr = _rec_out(crono, pain.id)
    assert rr is not None
    assert rr['total'] == 0
    assert rr['em_estoque'] == 0


def test_projecao_saldo_com_datas_e_producao(app):
    """O saldo expandido traz a projeção dia a dia: saídas DATADAS por loja,
    produção programada e o saldo do dia. Com estoque + produção, não falta."""
    l1 = _loja('Ribeiro')
    l2 = _loja('Anesio')
    r = _receita('Brioche')
    r.rendimento_qtd = 50
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=200))
    db.session.commit()
    d1 = hoje() + timedelta(days=1)
    _pedido(l1, 'pendente', d1, r, 550)
    _pedido(l2, 'pendente', d1, r, 500)

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr = _rec_out(crono, r.id)
    assert rr is not None
    assert len(rr['projecao']) == 7
    p1 = rr['projecao'][1]                           # dia da entrega (hoje+1)
    assert p1['saida'] == 1050
    assert {s['loja_nome'] for s in p1['saida_lojas']} == {'Ribeiro', 'Anesio'}
    assert p1['producao'] > 0                        # produção programada no dia
    assert all(p['saldo'] >= 0 for p in rr['projecao'])
    assert rr['dia_falta'] is None


def test_projecao_marca_dia_que_falta(app):
    """Entrega que o motor não consegue mais produzir a tempo (lead) é exibida
    como 'vai faltar' na projeção, mesmo o balanço a tendo excluído (zerada)."""
    loja = _loja()
    r = _receita('Pão Lead')
    r.dias_producao = 1                              # produz 1 dia antes
    db.session.commit()
    _pedido(loja, 'pendente', hoje(), r, 100)        # entrega HOJE: não dá tempo
    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr = _rec_out(crono, r.id)
    assert rr is not None
    assert rr['projecao'][0]['saida'] == 100
    assert rr['projecao'][0]['saldo'] == -100
    assert rr['dia_falta'] == rr['projecao'][0]['label']


def test_breakdown_bom_rastreia_insumo(app):
    """A massa (insumo) traz `breakdown_bom`: de QUAIS produtos finais a
    demanda dela veio. Responde 'de onde saiu esse número de massa?' — vem da
    produção do croissant × a receita, não foi digitado à mão."""
    from app.models import ReceitaIngrediente
    loja = _loja()
    massa = _receita('Massa para folhar')
    massa.dias_producao = 1
    cro = _receita('Croissant Tradicional')
    cro.rendimento_qtd = 50
    db.session.add(ReceitaIngrediente(
        receita_id=cro.id, tipo='receita', sub_receita_id=massa.id,
        ingrediente_nome='Massa para folhar', porcentagem=1))
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=3), cro, 100)

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rm = _rec_out(crono, massa.id)
    assert rm is not None and rm['insumo']
    assert rm['breakdown_bom']                          # rastreabilidade presente
    b = rm['breakdown_bom'][0]
    assert b['nome'] == 'Croissant Tradicional'
    assert b['pai_qtd'] == 100                          # 100 croissants produzidos
    assert b['qtd'] == 2                                # 100 × 1/50 = 2 un de massa
    # produto final NÃO tem breakdown_bom (só insumo)
    rc = _rec_out(crono, cro.id)
    assert rc['breakdown_bom'] == []


def test_projecao_expoe_previsto(app):
    """A projeção dia a dia expõe `previsto` (demanda do histórico pra a entrega
    daquele dia) — deixa ver quando a produção vem de pedido firme ou previsão."""
    loja = _loja()
    r = _receita('Pão')
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=2), r, 30)
    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr = _rec_out(crono, r.id)
    assert rr is not None
    assert all('previsto' in p for p in rr['projecao'])


def test_decompor_previsao_por_loja_e_dia(app):
    """decompor_previsao mostra de QUAL loja e dia vem o previsto: a soma do
    previsto por dia bate (de perto) com o previsto do cronograma, e a
    decomposição lista as lojas do histórico daquele dia-da-semana."""
    from app.services.previsao_producao import decompor_previsao
    l1 = _loja('Anesio')
    l2 = _loja('Ribeiro')
    r = _receita('Croissant')
    db.session.commit()
    # histórico: várias entregas no MESMO dia-da-semana (mesmo dow de hoje+2)
    alvo = hoje() + timedelta(days=2)
    for semanas in range(1, 5):                       # 4 ocorrências do dow
        dia = alvo - timedelta(days=7 * semanas)
        _pedido(l1, 'entregue', dia, r, 300)
        _pedido(l2, 'entregue', dia, r, 200)

    dec = decompor_previsao(r.id, horizonte_dias=7, janela_semanas=6,
                            inicio_offset_dias=0)
    assert dec is not None
    assert dec['receita']['nome'] == 'Croissant'
    dia2 = dec['dias'][2]                              # entrega no dow do histórico
    assert dia2['fonte'] == 'media_dow'
    assert dia2['previsto'] == 500                     # 300 + 200 (média estável)
    lojas = {x['loja_nome']: x['media'] for x in dia2['previsto_lojas']}
    assert lojas == {'Anesio': 300, 'Ribeiro': 200}
    assert any(h['loja_nome'] == 'Anesio' for h in dia2['historico'])


def test_rota_previsao_renderiza(app, admin_user):
    """A rota /telaindustriateste/previsao/<id> renderiza a decomposição."""
    loja = _loja('Anesio')
    r = _receita('Croissant')
    db.session.commit()
    alvo = hoje() + timedelta(days=2)
    for semanas in range(1, 4):
        _pedido(loja, 'entregue', alvo - timedelta(days=7 * semanas), r, 250)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/telaindustriateste/previsao/%d' % r.id)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'De onde vem a previsão' in html
    assert 'Anesio' in html


def test_rota_telaindustriateste(app, admin_user):
    loja = _loja()
    r = _receita()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 10)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/telaindustriateste/')
    assert resp.status_code == 200
    assert 'cronograma' in resp.get_data(as_text=True).lower()


def test_rota_renderiza_rastreabilidade_do_insumo(app, admin_user):
    """A tela renderiza o expandir do insumo com a origem (breakdown_bom) e a
    coluna Previsto — pro padeiro ver de onde sai a quantidade de massa."""
    from app.models import ReceitaIngrediente
    loja = _loja()
    massa = _receita('Massa para folhar')
    massa.dias_producao = 1
    cro = _receita('Croissant Tradicional')
    cro.rendimento_qtd = 50
    db.session.add(ReceitaIngrediente(
        receita_id=cro.id, tipo='receita', sub_receita_id=massa.id,
        ingrediente_nome='Massa para folhar', porcentagem=1))
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=3), cro, 100)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    html = client.get('/telaindustriateste/').get_data(as_text=True)
    assert 'Insumo puxado pela produção de' in html
    assert 'bom-box' in html
    assert '>Previsto<' in html


def test_aprovar_cria_plano_do_dia(app, admin_user):
    from app.models import PlanejamentoProducao
    from app.services.producao import aprovar_plano_do_dia

    loja = _loja()
    r = _receita()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 50)

    plano = aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
    assert plano is not None
    assert plano.data == d2
    assert plano.origem == 'cronograma'
    assert plano.status == 'aprovado'
    assert len(plano.itens) == 1
    it = plano.itens[0]
    assert it.receita_id == r.id
    assert it.qtd_alvo == 50
    assert it.produzido_qtd == 0
    assert PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').count() == 1


def test_reaprovar_substitui(app, admin_user):
    from app.models import PlanejamentoProducao
    from app.services.producao import aprovar_plano_do_dia

    loja = _loja()
    r = _receita()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 50)

    aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
    aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
    # re-aprovar nao duplica
    assert PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').count() == 1


def test_rota_aprovar(app, admin_user):
    from app.models import PlanejamentoProducao

    loja = _loja()
    r = _receita()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 30)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.post('/telaindustriateste/aprovar',
                       data={'data': d2.isoformat(), 'horizonte': 7,
                             'janela': 6})
    assert resp.status_code == 302
    assert PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').first() is not None


# ── fluxo 2 passos: aprovar (rascunho) -> enviar (padeiro vê) ──────────────────

def test_aprovar_cria_rascunho_padeiro_nao_ve(app, admin_user):
    """Aprovar cria a ordem como RASCUNHO (enviado_ao_padeiro=False); o padeiro
    NÃO vê (Produção do dia e Fluxograma vazios) até o passo 'enviar'."""
    from app.blueprints.padeiro.routes import _plano_do_dia
    from app.models import PlanejamentoProducao
    from app.services.gantt import montar_gantt
    from app.services.producao import aprovar_plano_do_dia

    loja = _loja()
    r = _receita()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 50)
    plano = aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
    assert plano.enviado_ao_padeiro is False        # rascunho
    assert _plano_do_dia(d2) is None                 # padeiro não vê
    assert montar_gantt(d2) is None
    # passo 2: enviar
    from app.services.producao import enviar_plano_do_dia
    enviar_plano_do_dia(d2)
    db.session.expire_all()
    assert PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').first().enviado_ao_padeiro is True
    assert _plano_do_dia(d2) is not None             # agora o padeiro vê


def test_rota_enviar(app, admin_user):
    """Enviar reconstrói a ordem a partir do grid (com overrides) e marca
    enviado — cria a ordem se não existir (dia futuro sem pedido vira produção
    num passo só)."""
    from app.models import PlanejamentoProducao
    loja = _loja()
    r = _receita('Pão Enviar')
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 30)
    c = app.test_client()
    c.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
           follow_redirects=True)
    resp = c.post('/telaindustriateste/enviar',
                  data={'data': d2.isoformat(), 'horizonte': 7, 'janela': 6,
                        'inicio': 0})
    assert resp.status_code in (302, 303)
    db.session.expire_all()
    plano = PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').first()
    assert plano is not None
    assert plano.enviado_ao_padeiro is True
    assert any(it.receita_id == r.id for it in plano.itens)


def test_enviar_reconstroi_do_grid_apos_edicao(app, admin_user):
    """Editar o grid DEPOIS de enviar e reenviar atualiza a produção do padeiro
    (a edição só chega no padeiro ao apertar enviar de novo)."""
    from app.models import PlanejamentoProducao
    from app.services.cronograma_edit import editar_celula
    from app.services.producao import enviar_plano_do_dia

    loja = _loja()
    r = _receita('Pão Reenvio')
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 30)

    enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7,
                        inicio_offset_dias=0)
    base = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr = _rec_out(base, r.id)
    # joga tudo no dia d2 via grid
    editar_celula(r.id, d2.isoformat(), rr['total'], horizonte_dias=7,
                  inicio_offset_dias=0)
    # reenvia: a ordem do padeiro passa a refletir o grid editado
    plano = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7,
                                inicio_offset_dias=0)
    db.session.expire_all()
    plano = PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').first()
    it = next(i for i in plano.itens if i.receita_id == r.id)
    assert it.qtd_alvo == 30
    assert plano.enviado_ao_padeiro is True


def test_editar_plano_espelha_no_grid(app, admin_user):
    """Mão dupla: editar a quantidade na tela 'editar plano' do padeiro salva o
    override e o grid da indústria passa a refletir aquele dia."""
    from app.models import CronogramaOverride, PlanejamentoProducao
    from app.services.producao import enviar_plano_do_dia

    loja = _loja()
    r = _receita('Pão Mão Dupla')
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 30)
    enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7,
                        inicio_offset_dias=0)
    plano = PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').first()
    it = next(i for i in plano.itens if i.receita_id == r.id)

    c = app.test_client()
    c.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
           follow_redirects=True)
    resp = c.post('/padeiro/plano/editar', data={
        'data': d2.isoformat(), 'alvo_%d' % it.id: 17}, follow_redirects=True)
    assert resp.status_code == 200

    ov = CronogramaOverride.query.filter_by(receita_id=r.id, data=d2).first()
    assert ov is not None and ov.qtd == 17
    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr = _rec_out(crono, r.id)
    cel = next(c for c in rr['por_dia'] if c['data'] == d2.isoformat())
    assert cel['qtd'] == 17


def test_enviar_preserva_produzido(app, admin_user):
    """Reenviar nunca baixa qtd_alvo abaixo do que o padeiro já produziu."""
    from app.services.producao import enviar_plano_do_dia

    loja = _loja()
    r = _receita('Pão Produzido')
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 30)
    plano = enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7,
                                inicio_offset_dias=0)
    it = next(i for i in plano.itens if i.receita_id == r.id)
    it.produzido_qtd = 50            # produziu mais que o alvo
    db.session.commit()
    # reenvia: alvo não pode cair abaixo de 50
    enviar_plano_do_dia(d2, admin_user.id, horizonte_dias=7,
                        inicio_offset_dias=0)
    db.session.expire_all()
    it2 = next(i for i in plano.itens if i.receita_id == r.id)
    assert it2.qtd_alvo >= 50


def test_plano_antigo_sem_flag_continua_visivel(app, admin_user):
    """Ordem antiga (criada sem o flag) tem DEFAULT True -> padeiro continua
    vendo (não quebra produção em andamento)."""
    from app.blueprints.padeiro.routes import _plano_do_dia
    from app.models import PlanejamentoItem, PlanejamentoProducao
    r = _receita('Pão Antigo')
    plano = PlanejamentoProducao(data=hoje(), origem='cronograma')  # sem flag
    db.session.add(plano); db.session.flush()
    db.session.add(PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                                    multiplicador=1, qtd_alvo=10))
    db.session.commit()
    assert _plano_do_dia(hoje()) is not None         # default True -> visível


# ── excluir ordem de produção enviada (desfazer envio errado) ──────────────

def test_excluir_plano_enviado_sem_producao(app, admin_user):
    from app.models import PlanejamentoProducao
    from app.services.producao import aprovar_plano_do_dia, enviar_plano_do_dia, excluir_plano_do_dia
    loja = _loja()
    r = _receita()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 50)
    aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
    enviar_plano_do_dia(d2)

    res = excluir_plano_do_dia(d2)
    assert res['ok'] is True
    assert PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').first() is None


def test_excluir_bloqueia_se_ja_produziu(app, admin_user):
    from app.models import PlanejamentoProducao
    from app.services.producao import aprovar_plano_do_dia, excluir_plano_do_dia
    loja = _loja()
    r = _receita()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 50)
    plano = aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
    plano.itens[0].produzido_qtd = 5            # padeiro ja produziu parte
    db.session.commit()

    res = excluir_plano_do_dia(d2)
    assert res['ok'] is False
    assert res['erro'] == 'ja_produzido' and res['produzido'] == 5
    # plano preservado (a producao real ja mexeu no estoque/MP)
    assert PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').first() is not None


def test_excluir_inexistente(app):
    from app.services.producao import excluir_plano_do_dia
    res = excluir_plano_do_dia(hoje() + timedelta(days=9))
    assert res['ok'] is False and res['erro'] == 'nao_encontrado'


def test_rota_excluir(app, admin_user):
    from app.models import PlanejamentoProducao
    from app.services.producao import aprovar_plano_do_dia, enviar_plano_do_dia
    loja = _loja()
    r = _receita()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 30)
    aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
    enviar_plano_do_dia(d2)

    c = app.test_client()
    c.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
           follow_redirects=True)
    resp = c.post('/telaindustriateste/excluir',
                  data={'data': d2.isoformat(), 'horizonte': 7, 'janela': 6})
    assert resp.status_code == 302
    assert PlanejamentoProducao.query.filter_by(
        data=d2, origem='cronograma').first() is None

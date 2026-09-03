"""Cronograma de producao POR DIA (previsao_producao.cronograma_producao) +
rota /telaindustriateste.

Distribui a producao por dia acompanhando as entregas (deslocado pelo lead),
descontando o estoque dos primeiros dias.
"""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import EstoqueProducao, Loja, PedidoItem, PedidoLoja, Receita
from app.services.previsao_producao import cronograma_producao
from app.utils import hoje


@pytest.fixture(autouse=True)
def _hoje_e_segunda_fixa(congela_hoje):
    """Producao so seg-sex (dono 17/08/2026) tornou o shaping do cronograma
    sensivel ao dia da semana — congela hoje() numa SEGUNDA pros cenarios
    hoje()+N deste arquivo cairem sempre em dia util, em qualquer dia em que
    a suite rode (ver conftest.congela_hoje)."""
    congela_hoje()


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


def test_equilibrar_capacidade_independente_dos_dois_padeiros(app):
    """Pão e viennoiserie ocupam filas humanas diferentes no nivelamento.

    Os dois podem preencher o mesmo dia ocioso; a carga de croissant não deve
    impedir o padeiro de pães de adiantar Brioche.
    """
    loja = _loja()
    brioche = _receita_amassadeira(
        'Brioche', rend=50, peso_base=5000, cap=500)
    croissant = _receita_amassadeira(
        'Croissant', rend=50, peso_base=5000, cap=500)
    croissant.categoria = 'Viennoiserie'
    croissant.familia = 'viennoiserie'
    db.session.commit()
    amanha = hoje() + timedelta(days=1)
    _pedido(loja, 'pendente', amanha, brioche, 30)
    _pedido(loja, 'pendente', amanha, croissant, 30)

    eq = cronograma_producao(
        horizonte_dias=2, inicio_offset_dias=0, equilibrar=True)
    brioche_out = _rec_out(eq, brioche.id)
    croissant_out = _rec_out(eq, croissant.id)

    assert brioche_out['por_dia'][0]['qtd'] > 0
    assert croissant_out['por_dia'][0]['qtd'] > 0
    assert brioche_out['total'] == croissant_out['total'] == 30


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
    """Fornada especial (vende sáb/dom — dono 10/08/2026) com lead 1:
    produzida na SEXTA pra venda no SÁBADO. A regra restringe a VENDA; o
    lead desloca a produção pra véspera — então a sexta NÃO é bloqueada."""
    loja = _loja()
    r = _receita('Focaccia')
    r.fornada_especial = True
    r.dias_producao = 1
    db.session.commit()
    hoje_d = hoje()
    sabado = next(hoje_d + timedelta(days=i) for i in range(1, 14)
                  if (hoje_d + timedelta(days=i)).weekday() == 5)
    sexta = sabado - timedelta(days=1)
    _pedido(loja, 'pendente', sabado, r, 50)          # entrega no sábado
    horizonte = (sabado - hoje_d).days + 1
    crono = cronograma_producao(horizonte_dias=horizonte, inicio_offset_dias=0)
    rr = _rec_out(crono, r.id)
    assert rr is not None
    por_data = {c['data']: c['qtd'] for c in rr['por_dia']}
    assert por_data.get(sexta.isoformat(), 0) > 0     # produz na sexta
    assert por_data.get(sabado.isoformat(), 0) == 0   # não no sábado


# ── motor de previsão: pedidos | vendas | maior (dono, 06/07/2026) ──────────
# "+1 opção de previsão de produção, baseada nas vendas": o previsto pode vir
# do histórico de PEDIDOS (original), da VENDA real das lojas (+ merma) ou do
# MAIOR dos dois por dia. O firme conta sempre.

def _vendas_no_dow(receita, loja, qtd, semanas=4, dow=None):
    """Semeia vendas semanais no mesmo dia-da-semana (default: o dow de
    hoje+2, pra previsão cair no horizonte)."""
    from datetime import datetime as _dt
    from datetime import time as _time

    from app.models import EstoqueLoja, MovEstoqueLoja
    el = EstoqueLoja.query.filter_by(loja_id=loja.id,
                                     receita_id=receita.id).first()
    if el is None:
        el = EstoqueLoja(loja_id=loja.id, receita_id=receita.id, quantidade=0)
        db.session.add(el)
        db.session.commit()
    alvo = hoje() + timedelta(days=2)
    if dow is not None:
        while alvo.weekday() != dow:
            alvo += timedelta(days=1)
    for sem in range(1, semanas + 1):
        d = alvo - timedelta(days=7 * sem)
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo='venda_seru', quantidade=qtd,
            data=_dt.combine(d, _time(12, 0)), referencia='teste-motor'))
    db.session.commit()
    return alvo


def test_balanco_motor_vendas_preve_pela_venda(app):
    """Receita que VENDE toda semana mas nunca teve pedido: o motor 'pedidos'
    não vê nada; o motor 'vendas' prevê e manda produzir."""
    from app.services.previsao_producao import balanco_industria

    loja = _loja()
    r = _receita('Croissant Venda')
    _vendas_no_dow(r, loja, 40)

    bal_p = balanco_industria(horizonte_dias=7, usar_cache=False,
                              motor='pedidos')
    item_p = next((i for i in bal_p['itens'] if i['receita_id'] == r.id), None)
    assert item_p is None or item_p['previsto'] == 0

    bal_v = balanco_industria(horizonte_dias=7, usar_cache=False,
                              motor='vendas')
    assert bal_v['motor'] == 'vendas'
    item_v = next(i for i in bal_v['itens'] if i['receita_id'] == r.id)
    assert item_v['previsto'] > 0
    assert item_v['produzir'] > 0          # sem estoque, produz pra cobrir


def test_cronograma_motor_vendas_produz_no_dia_da_venda(app):
    """A curva diária segue a venda: vendeu sempre no dow X → produção cai no
    dia cuja entrega é X (lead 0)."""
    loja = _loja()
    r = _receita('Pão Venda Dia')
    alvo = _vendas_no_dow(r, loja, 30)

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0,
                                motor='vendas')
    assert crono['motor'] == 'vendas'
    rr = _rec_out(crono, r.id)
    assert rr is not None
    por_data = {c['data']: c['qtd'] for c in rr['por_dia']}
    assert por_data.get(alvo.isoformat(), 0) > 0


def test_motor_maior_usa_o_maior_dos_dois(app):
    """Pedidos de 20/semana e venda de 50/semana no MESMO dow → 'maior'
    prevê pelo menos o previsto de vendas (nunca menos que qualquer motor)."""
    from app.services.previsao_producao import balanco_industria

    loja = _loja()
    r = _receita('Pão Maior')
    alvo = _vendas_no_dow(r, loja, 50)
    for sem in (1, 2, 3, 4):
        _pedido(loja, 'entregue', alvo - timedelta(days=7 * sem), r, 20)

    def _prev(motor):
        bal = balanco_industria(horizonte_dias=7, usar_cache=False,
                                motor=motor)
        it = next((i for i in bal['itens'] if i['receita_id'] == r.id), None)
        return it['previsto'] if it else 0

    prev_p, prev_v, prev_m = _prev('pedidos'), _prev('vendas'), _prev('maior')
    assert prev_v > prev_p > 0
    assert prev_m >= max(prev_p, prev_v)


def test_decompor_motor_vendas_marca_origem(app):
    from app.services.previsao_producao import decompor_previsao

    loja = _loja()
    r = _receita('Pão Decompor Venda')
    _vendas_no_dow(r, loja, 25)
    dec = decompor_previsao(r.id, horizonte_dias=7, inicio_offset_dias=0,
                            motor='vendas')
    assert dec['motor'] == 'vendas'
    assert dec['total_previsto'] > 0
    assert all(d['origem'] == 'vendas' for d in dec['dias'])


def test_rota_cronograma_motor_vendas_renderiza(app, admin_user):
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
    loja = _loja()
    r = _receita('Croissant Motor UI')
    _vendas_no_dow(r, loja, 40)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    html = client.get('/telaindustriateste/?motor=vendas').get_data(as_text=True)
    assert 'previsão por VENDAS' in html          # badge do motor ativo
    assert 'name="motor"' in html                 # select + hidden dos forms
    assert 'Croissant Motor UI' in html


def test_tela_abre_no_motor_vendas_por_default(app, admin_user):
    """Decisão do dono 17/08/2026 ("mesma régua em tudo"): SEM ?motor= a
    tela abre em VENDAS — mesma régua da automação (auto-envio + 🔄). URL
    antiga/bookmark sem o param cai no motor novo."""
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
    loja = _loja()
    r = _receita('Croissant Default Vendas')
    _vendas_no_dow(r, loja, 40)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    html = client.get('/telaindustriateste/').get_data(as_text=True)
    assert 'previsão por VENDAS' in html          # badge = motor ativo
    # receita SÓ com venda (sem pedido histórico) aparece no grid default
    assert 'Croissant Default Vendas' in html
    # o motor viaja explícito nos forms de ordem do dia (aprovar/enviar):
    # escolher 'pedidos' no select também é preservado no POST.
    assert 'name="motor" value="vendas"' in html


def test_tela_abre_equilibrada_por_default(app, admin_user):
    """Dono 17/08/2026 ("o sistema deve equilibrar sozinho"): sem parâmetro a
    tela abre com a carga EQUILIBRADA e o valor viaja explícito nos forms;
    escolher 'pela demanda' (equilibrar=0) é preservado."""
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
    _loja()
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    html = client.get('/telaindustriateste/').get_data(as_text=True)
    assert 'value="1" selected>equilibrada' in html
    assert 'name="equilibrar" value="1"' in html       # hidden dos forms
    html2 = client.get('/telaindustriateste/?equilibrar=0').get_data(
        as_text=True)
    assert 'value="0" selected>pela demanda' in html2
    assert 'name="equilibrar" value="0"' in html2      # escolha preservada


def test_default_nivelado_espalha_pra_dias_ocupados(app):
    """Sem equilibrar explícito, o cronograma nivela sozinho: demanda só na
    QUINTA (com seg-qua ociosos) é ADIANTADA — a produção não fica empilhada
    no último dia possível."""
    loja = _loja()
    r = _receita('Pão Nivelado')
    quinta = hoje() + timedelta(days=3)            # hoje congelado = segunda
    _pedido(loja, 'pendente', quinta, r, 50)

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0,
                                equilibrar=True)
    rr = _rec_out(crono, r.id)
    assert rr is not None and rr['total'] == 50
    por_data = {c['data']: c['qtd'] for c in rr['por_dia']}
    # a produção foi puxada pra dias ANTERIORES ao deadline (nunca depois —
    # a entrega de quinta continua garantida)
    dia_prod = next(d for d, q in por_data.items() if q > 0)
    assert dia_prod <= quinta.isoformat()


def test_nivelamento_respeita_antecedencia_maxima(app):
    """Caso Brioche (dono 17/08/2026 à noite: "vence em 3 dias, não é
    congelado"): demanda DIÁRIA da semana NÃO vira um dia-monstro na
    segunda — cada lote é produzido no máximo _ANTECEDENCIA_MAX_DIAS antes
    da necessidade (3 desde "Tem que adiantar" da mesma noite). Invariante:
    o acumulado produzido até o dia D nunca passa do acumulado de demanda
    até D+antecedência."""
    from datetime import date as _date

    from app.services.previsao_producao import _ANTECEDENCIA_MAX_DIAS

    loja = _loja()
    r = _receita('Brioche Fresco')
    hoje_d = hoje()                                # segunda congelada
    demanda = {}                                   # dia -> qtd firme
    for i in range(1, 7):                          # ter..dom, 20/dia
        d = hoje_d + timedelta(days=i)
        _pedido(loja, 'pendente', d, r, 20)
        demanda[d.isoformat()] = 20

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0,
                                equilibrar=True)
    rr = _rec_out(crono, r.id)
    assert rr is not None and rr['total'] >= 100
    datas = [c['data'] for c in rr['por_dia']]
    prod_acum = 0
    ant = _ANTECEDENCIA_MAX_DIAS
    for idx, c in enumerate(rr['por_dia']):
        prod_acum += c['qtd']
        dem_ate = sum(q for d_iso, q in demanda.items()
                      if d_iso <= datas[min(idx + ant, len(datas) - 1)])
        assert prod_acum <= dem_ate + 1e-9, (
            'produção adiantada além de %d dias da necessidade em %s'
            % (ant, c['data']))
        if c['qtd']:
            assert _date.fromisoformat(c['data']).weekday() < 5


def test_nivelamento_nao_antecipa_parcela_do_fim_de_semana(app):
    """A demanda de SÁB/DOM já rolou pra sexta (produção seg-sex), mas a
    antecedência é medida POR PARCELA contra o dia de DEMANDA original
    (ref_pesos): com 3 dias, a parcela de SÁBADO pode ir até quarta e a de
    DOMINGO até quinta. Nada em seg/ter — pão de sábado assado na terça
    teria 4 dias."""
    from datetime import date as _date

    loja = _loja()
    r = _receita('Pão Fresco FDS')
    hoje_d = hoje()                                # segunda congelada
    sabado = hoje_d + timedelta(days=5)
    domingo = hoje_d + timedelta(days=6)
    _pedido(loja, 'pendente', sabado, r, 40)
    _pedido(loja, 'pendente', domingo, r, 40)

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0,
                                equilibrar=True)
    rr = _rec_out(crono, r.id)
    assert rr is not None and rr['total'] >= 80
    for c in rr['por_dia']:
        d = _date.fromisoformat(c['data'])
        if c['qtd']:
            # qua (sáb−3) a sex são os únicos válidos; seg/ter nunca
            assert d.weekday() in (2, 3, 4), \
                'parcela do FDS adiantada pra %s' % d


def test_nivelamento_redistribui_excedente_da_sexta(app):
    """Caso real 17/08 (dono: "Esta assim ainda"): o teto alvo=total/dias
    uteis deixava a cota dos dias que o frescor impede de receber morrer
    e os paes empilhavam TODOS na sexta. Sem o teto, o excedente da sexta
    se redistribui pelos dias anteriores que a antecedencia (3) alcanca —
    a parcela de sexta pode ir ate terca, a de sabado ate quarta, a de
    domingo ate quinta; segunda nunca."""
    from datetime import date as _date

    loja = _loja()
    r = _receita('Pão de Fim de Semana')
    hoje_d = hoje()                                # segunda congelada
    _pedido(loja, 'pendente', hoje_d + timedelta(days=4), r, 30)   # sex
    _pedido(loja, 'pendente', hoje_d + timedelta(days=5), r, 30)   # sáb
    _pedido(loja, 'pendente', hoje_d + timedelta(days=6), r, 30)   # dom

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0,
                                equilibrar=True)
    rr = _rec_out(crono, r.id)
    assert rr is not None and rr['total'] >= 90
    por_wd = {}
    for c in rr['por_dia']:
        if c['qtd']:
            por_wd[_date.fromisoformat(c['data']).weekday()] = c['qtd']
    assert set(por_wd) <= {1, 2, 3, 4}       # segunda nunca (sex − 3 = ter)
    assert sum(por_wd.values()) == rr['total']
    assert len(por_wd) >= 3                  # redistribuido, nao empilhado


def test_nivelamento_nao_deixa_celula_farelo(app):
    """Caso real 18/08 (dono: "2 paes e ridiculo, deveria ser nenhum de
    nozes e azeitonas"): parcela minuscula (2 un de sexta) movida pra um
    dia vazio virava celula-farelo — ninguem acende o forno por 2 paes.
    A consolidacao anti-farelo funde a celula menor que a fracao minima
    de fornada numa celula ja existente do item (no prazo e no frescor)."""
    from datetime import date as _date

    loja = _loja()
    r = _receita('Sourdough Nozes Farelo')
    hoje_d = hoje()                                # segunda congelada
    _pedido(loja, 'pendente', hoje_d + timedelta(days=4), r, 2)     # sex
    _pedido(loja, 'pendente', hoje_d + timedelta(days=6), r, 118)   # dom

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0,
                                equilibrar=True)
    rr = _rec_out(crono, r.id)
    assert rr is not None and rr['total'] >= 120
    qtds = {c['data']: c['qtd'] for c in rr['por_dia'] if c['qtd']}
    # nenhuma celula-farelo: tudo que sobrou e >= 2 digitos de fornada
    assert all(q >= 4 for q in qtds.values()), qtds
    assert sum(qtds.values()) == rr['total']
    # e o total nao passou do prazo: nada DEPOIS do dia de demanda
    for d_iso in qtds:
        assert _date.fromisoformat(d_iso).weekday() <= 4


def test_nivelamento_pesa_pelo_lote_de_producao(app):
    """Caso real 18/08 (dono: "101 brioches e pra acabar"): a regua de
    peso usava a fornada TEORICA da capacidade da amassadeira (~448
    brioches) — 101 valiam 0.2 fornada, mover brioche era "de graca" pro
    guard e o nivelador empilhava tudo no primeiro dia alcancavel. Com o
    peso pelo `lote_producao` do cadastro (o lote real do dono, 10),
    cada batida pesa 1 e a demanda diaria fica ESPALHADA."""
    loja = _loja()
    r = _receita('Brioche Lote Pequeno')
    r.lote_producao = 10
    r.capacidade_amassadeira_g = 112000            # fornada teorica enorme
    db.session.commit()
    hoje_d = hoje()                                # segunda congelada
    for i in range(1, 7):                          # ter..dom, 32/dia
        _pedido(loja, 'pendente', hoje_d + timedelta(days=i), r, 32)

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0,
                                equilibrar=True)
    rr = _rec_out(crono, r.id)
    assert rr is not None and rr['total'] >= 190
    qtds = [c['qtd'] for c in rr['por_dia']]
    assert max(qtds) <= 70, qtds                   # nada de dia-monstro
    assert sum(1 for q in qtds if q > 0) >= 3      # espalhado na semana


def test_dribble_respeita_lote_de_producao(app):
    """Caso "101 brioches e pra acabar", parte 2: o minimo do dribble
    vinha da fornada TEORICA da capacidade da amassadeira (~448 un →
    minimo 90) e, como o sumidouro da consolidacao e o dia 0, a demanda
    diaria de ~30 cascateava INTEIRA pra hoje. Com `lote_producao`
    definido (10) o minimo e 2 e a demanda diaria fica em pe no proprio
    dia."""
    loja = _loja()
    r = _receita_amassadeira('Brioche Dribble', rend=4, peso_base=1000,
                             cap=112000)           # fornada teorica ~448
    r.lote_producao = 10
    db.session.commit()
    hoje_d = hoje()                                # segunda congelada
    for i in range(1, 5):                          # ter..sex, 30/dia
        _pedido(loja, 'pendente', hoje_d + timedelta(days=i), r, 30)

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0,
                                equilibrar=False)  # dribble puro
    rr = _rec_out(crono, r.id)
    assert rr is not None and rr['total'] >= 120
    qtds = [c['qtd'] for c in rr['por_dia']]
    assert max(qtds) <= 60, qtds                   # nada cascateou pro dia 0
    assert sum(1 for q in qtds if q > 0) >= 3


def test_nivelamento_fatia_receita_grande_em_lotes(app):
    """Caso Croissant (dono: "por que não redistribuir em lotes menores?"):
    demanda grande num dia só é FATIADA — nenhum dia carrega o total
    inteiro quando há dias anteriores dentro da antecedência."""
    loja = _loja()
    r = _receita('Croissant Lote')
    r.lote_producao = 100                          # lotes de 100
    db.session.commit()
    quinta = hoje() + timedelta(days=3)            # segunda congelada
    _pedido(loja, 'pendente', quinta, r, 300)

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0,
                                equilibrar=True)
    rr = _rec_out(crono, r.id)
    assert rr is not None and rr['total'] >= 300
    qtds = [c['qtd'] for c in rr['por_dia']]
    assert max(qtds) < 300                         # fatiado, sem dia-monstro
    assert sum(1 for q in qtds if q > 0) >= 2      # espalhado em 2+ dias


def test_escolher_pedidos_no_select_e_preservado(app, admin_user):
    """Com o default em vendas, quem escolhe 'Pedidos das lojas' no select
    não pode ser devolvido pra 'vendas' no redirect/POST — o motor agora
    viaja SEMPRE explícito (hidden + _params_visao)."""
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
    _loja()
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    html = client.get('/telaindustriateste/?motor=pedidos').get_data(
        as_text=True)
    assert 'previsão por VENDAS' not in html      # badge só fora do pedidos
    assert 'name="motor" value="pedidos"' in html  # hidden preserva a escolha


def test_enviar_com_motor_vendas_usa_a_grade_de_vendas(app, admin_user):
    """Enviar ao padeiro com motor=vendas cria a ordem a partir da grade de
    VENDAS — receita sem pedido histórico entra no plano (no motor
    'pedidos' ela nem apareceria)."""
    from app.models import PlanejamentoItem, PlanejamentoProducao

    loja = _loja()
    r = _receita('Pão Enviar Venda')
    alvo = _vendas_no_dow(r, loja, 35)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.post('/telaindustriateste/enviar',
                       data={'data': alvo.isoformat(), 'horizonte': 7,
                             'janela': 6, 'inicio': 0, 'motor': 'vendas',
                             'equilibrar': 0})          # modo curva explícito
    assert resp.status_code in (302, 303)
    assert '/telaindustriateste/' in resp.headers['Location']
    assert 'motor=vendas' in resp.headers['Location']   # visão preservada
    plano = PlanejamentoProducao.query.filter_by(
        data=alvo, origem='cronograma').first()
    assert plano is not None and plano.enviado_ao_padeiro is True
    item = PlanejamentoItem.query.filter_by(planejamento_id=plano.id,
                                            receita_id=r.id).first()
    assert item is not None and item.qtd_alvo > 0


# ── fornada especial: PRODUÇÃO só sex/sáb (dono 10/08/2026; antes qui/sex/
# sáb desde 06/07). A venda de sáb/dom sai da véspera (sex→sáb, sáb→dom); o
# cronograma nunca programa (nem deixa editar) produção nos demais dias.

def _proximo_dow(base, dow):
    return next(base + timedelta(days=i) for i in range(1, 15)
                if (base + timedelta(days=i)).weekday() == dow)


def test_fornada_especial_lead0_venda_domingo_produz_sabado(app):
    """Venda de DOMINGO com lead 0 caía no próprio domingo — produção de
    fornada especial vai pra véspera (sábado)."""
    loja = _loja()
    r = _receita('Focaccia')
    r.fornada_especial = True                     # lead 0 (default)
    db.session.commit()
    hoje_d = hoje()
    domingo = _proximo_dow(hoje_d, 6)
    sabado = domingo - timedelta(days=1)
    _pedido(loja, 'pendente', domingo, r, 40)

    horizonte = (domingo - hoje_d).days + 1
    crono = cronograma_producao(horizonte_dias=horizonte, inicio_offset_dias=0)
    rr = _rec_out(crono, r.id)
    assert rr is not None
    por_data = {c['data']: c['qtd'] for c in rr['por_dia']}
    assert por_data.get(domingo.isoformat(), 0) == 0     # dom não produz
    assert por_data.get(sabado.isoformat(), 0) >= 40     # sai da véspera


def test_fornada_especial_nunca_produz_fora_sex_sab(app):
    """Nem pedido firme aberrante numa TERÇA faz o cronograma programar dia
    útil: a produção só cai em sex/sáb; sem dia permitido antes da
    entrega, a linha não produz (a falta vira alerta de entrega em risco —
    decisão humana, não do cronograma)."""
    from datetime import date as _date

    loja = _loja()
    r = _receita('Focaccia')
    r.fornada_especial = True
    db.session.commit()
    _pedido(loja, 'pendente', _proximo_dow(hoje(), 1), r, 30)   # terça

    crono = cronograma_producao(horizonte_dias=14, inicio_offset_dias=0)
    rr = _rec_out(crono, r.id)
    assert rr is not None
    for c in rr['por_dia']:
        if c['qtd']:
            assert _date.fromisoformat(c['data']).weekday() in (4, 5)


def test_equilibrar_nao_adianta_fornada_especial_pra_dia_util(app):
    """'Equilibrar carga' nivelava puxando receita pra dia ocioso — fornada
    especial não pode ser adiantada pra seg/ter/qua/dom."""
    from datetime import date as _date

    loja = _loja()
    r = _receita('Focaccia')
    r.fornada_especial = True
    db.session.commit()
    _pedido(loja, 'pendente', _proximo_dow(hoje(), 5), r, 40)   # sábado

    crono = cronograma_producao(horizonte_dias=14, inicio_offset_dias=0,
                                equilibrar=True)
    rr = _rec_out(crono, r.id)
    assert rr is not None and rr['total'] > 0
    for c in rr['por_dia']:
        if c['qtd']:
            assert _date.fromisoformat(c['data']).weekday() in (4, 5)


def test_editar_celula_recusa_dia_bloqueado(app):
    """Editar célula de fornada especial num dia bloqueado é recusado ANTES de
    salvar (defesa em profundidade — a tela já trava a célula)."""
    from app.models import CronogramaOverride
    from app.services.cronograma_edit import editar_celula

    loja = _loja()
    r = _receita('Focaccia')
    r.fornada_especial = True
    db.session.commit()
    _pedido(loja, 'pendente', _proximo_dow(hoje(), 4), r, 40)   # na grade

    segunda = _proximo_dow(hoje(), 0)
    res = editar_celula(r.id, segunda.isoformat(), 25,
                        horizonte_dias=14, inicio_offset_dias=0)
    assert res['erro'] == 'dia_bloqueado'
    assert CronogramaOverride.query.count() == 0     # nada salvo
    # dia permitido continua editável
    sexta = _proximo_dow(hoje(), 4)
    res2 = editar_celula(r.id, sexta.isoformat(), 25,
                         horizonte_dias=14, inicio_offset_dias=0)
    assert res2 is not None and 'erro' not in res2


def test_rota_celula_dia_bloqueado_422(app, admin_user):
    loja = _loja()
    r = _receita('Focaccia')
    r.fornada_especial = True
    db.session.commit()
    _pedido(loja, 'pendente', _proximo_dow(hoje(), 4), r, 40)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'})
    resp = client.post('/telaindustriateste/celula', json={
        'receita_id': r.id, 'data': _proximo_dow(hoje(), 0).isoformat(),
        'qtd': 25, 'horizonte': 14, 'janela': 6, 'inicio': 0, 'equilibrar': 0})
    assert resp.status_code == 422
    j = resp.get_json()
    assert j['ok'] is False and j['erro'] == 'dia_bloqueado'


def test_fornada_especial_celula_bloqueada_na_tela(app, admin_user):
    """A tela marca as células de seg/ter/qua/dom da fornada especial
    (hachura + readonly) e mostra o badge 'fim de semana'."""
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
    loja = _loja()
    r = _receita('Focaccia')
    r.fornada_especial = True
    db.session.commit()
    _pedido(loja, 'pendente', _proximo_dow(hoje(), 4), r, 40)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    html = client.get('/telaindustriateste/?horizonte=7').get_data(as_text=True)
    assert 'cel-bloq' in html            # horizonte de 7 dias sempre tem seg-qua
    assert 'fim de semana' in html
    assert 'produz só sexta/sábado' in html


# ── produção NORMAL só de segunda a sexta (dono 17/08/2026): "Sábado e
# domingo a gente não produz, jogar tudo para segunda a sexta, a única coisa
# que produzimos de sábado é a fornada especial". A demanda de sáb/dom rola
# pro último dia permitido anterior (sexta), na receita final E no insumo. ──

def test_receita_normal_nao_produz_no_fim_de_semana(app):
    """Pedidos firmes de SÁBADO e DOMINGO numa receita comum: nenhuma célula
    de fim de semana produz — tudo rola pra SEXTA. As células de sáb/dom
    saem bloqueadas pra tela."""
    from datetime import date as _date

    loja = _loja()
    r = _receita('Pao de Semana')
    hoje_d = hoje()
    domingo = _proximo_dow(hoje_d, 6)
    sexta = domingo - timedelta(days=2)
    assert sexta >= hoje_d               # garantido pelo hoje() congelado (seg)
    _pedido(loja, 'pendente', domingo, r, 40)
    _pedido(loja, 'pendente', domingo - timedelta(days=1), r, 30)   # sábado

    horizonte = (domingo - hoje_d).days + 1
    crono = cronograma_producao(horizonte_dias=horizonte, inicio_offset_dias=0)
    rr = _rec_out(crono, r.id)
    assert rr is not None and rr['total'] >= 70          # nada se perde
    por_data = {c['data']: c['qtd'] for c in rr['por_dia']}
    for c in rr['por_dia']:
        d = _date.fromisoformat(c['data'])
        if c['qtd']:
            assert d.weekday() < 5                       # só dia útil
        if d.weekday() >= 5:
            assert c.get('bloqueado') is True            # tela trava a célula
    assert por_data.get(sexta.isoformat(), 0) >= 70      # sáb+dom saem de sexta


def test_insumo_vespera_de_segunda_nao_cai_no_domingo(app):
    """Croissant pedido pra SEGUNDA com massa de lead 1: a véspera cairia no
    DOMINGO — a produção do insumo rola pra SEXTA (produzir mais cedo chega
    a tempo); a linha do insumo também não produz em fim de semana."""
    from datetime import date as _date

    from app.models import ReceitaIngrediente

    loja = _loja()
    massa = _receita('Massa FDS')
    massa.dias_producao = 1
    cro = _receita('Croissant FDS')
    cro.rendimento_qtd = 50
    db.session.add(ReceitaIngrediente(
        receita_id=cro.id, tipo='receita', sub_receita_id=massa.id,
        ingrediente_nome='Massa FDS', porcentagem=1))
    db.session.commit()
    hoje_d = hoje()
    segunda = _proximo_dow(hoje_d, 0)
    assert segunda - timedelta(days=3) >= hoje_d         # sexta no grid
    _pedido(loja, 'pendente', segunda, cro, 100)

    horizonte = (segunda - hoje_d).days + 1
    crono = cronograma_producao(horizonte_dias=horizonte, inicio_offset_dias=0)
    rm = _rec_out(crono, massa.id)
    assert rm is not None and rm['total'] >= 2           # 100 × (1/50)
    for c in rm['por_dia']:
        if c['qtd']:
            assert _date.fromisoformat(c['data']).weekday() < 5


def test_editar_celula_recusa_fim_de_semana_receita_normal(app):
    """Editar célula de RECEITA COMUM no sábado é recusado (dia_bloqueado,
    mensagem própria — não a da fornada); sexta segue editável."""
    from app.models import CronogramaOverride
    from app.services.cronograma_edit import editar_celula

    loja = _loja()
    r = _receita('Pao Semana Edit')
    _pedido(loja, 'pendente', _proximo_dow(hoje(), 3), r, 40)     # na grade

    sabado = _proximo_dow(hoje(), 5)
    res = editar_celula(r.id, sabado.isoformat(), 25,
                        horizonte_dias=14, inicio_offset_dias=0)
    assert res['erro'] == 'dia_bloqueado'
    assert 'segunda a sexta' in res['msg']
    assert CronogramaOverride.query.count() == 0         # nada salvo
    sexta = _proximo_dow(hoje(), 4)
    res2 = editar_celula(r.id, sexta.isoformat(), 25,
                         horizonte_dias=14, inicio_offset_dias=0)
    assert res2 is not None and 'erro' not in res2


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


def test_projecao_credita_producao_no_dia_pronto(app):
    """C2: na projeção de saldo, a produção entra no estoque quando fica PRONTA
    (dia de início + lead), não no dia em que começa. Receita lead 2, entrega 30
    em hoje+3: produz em hoje+1 (mira a entrega), pronta em hoje+3. O saldo NÃO
    sobe em hoje+1 (produção ainda em andamento) — só em hoje+3, junto da saída."""
    loja = _loja()
    r = _receita('Pão de fermentação longa')
    r.dias_producao = 2
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=3), r, 30)

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr = _rec_out(crono, r.id)
    assert rr is not None
    assert rr['por_dia'][1]['qtd'] == 30           # produção INICIA em hoje+1
    proj = rr['projecao']
    # nada pronto ainda nos dias 1 e 2 -> saldo fica em 0 (não sobe cedo demais).
    assert proj[1]['producao'] == 0 and proj[1]['saldo'] == 0
    assert proj[2]['producao'] == 0 and proj[2]['saldo'] == 0
    # pronto em hoje+3: entra +30 no MESMO dia da saída de 30 -> saldo 0.
    assert proj[3]['producao'] == 30
    assert proj[3]['saida'] == 30 and proj[3]['saldo'] == 0


def test_projecao_lead_revela_falta_escondida(app):
    """C2: creditar a produção no dia de INÍCIO escondia falta. Receita lead 2,
    entrega 10 em hoje+1 (impossível produzir a tempo — teria de iniciar antes do
    horizonte): a projeção acusa a falta em hoje+1, não a mascara com produção que
    só fica pronta depois."""
    loja = _loja()
    r = _receita('Pão longo')
    r.dias_producao = 2
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 10)
    _pedido(loja, 'pendente', hoje() + timedelta(days=3), r, 10)

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr = _rec_out(crono, r.id)
    proj = rr['projecao']
    assert proj[1]['saldo'] < 0                     # falta em hoje+1 (não escondida)
    assert rr['dia_falta'] == proj[1]['label']


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


def test_insumo_mostra_estoque_real_batido(app):
    """REGRESSÃO: a massa (insumo) já BATIDA que cobre a demanda deve mostrar o
    estoque REAL na linha — não 'em estoque: 0'. Antes `_explodir_bom` criava a
    linha com em_estoque=est_extra (=0 pra sub que está no balanço por ter
    estoque), então a massa batida aparecia como 0 e parecia bug, apesar de a
    produção 0 estar certa (o estoque cobre a demanda dos croissants)."""
    from app.models import EstoqueProducao, ReceitaIngrediente
    loja = _loja()
    massa = _receita('Massa para folhar')
    massa.dias_producao = 1
    cro = _receita('Croissant Tradicional')
    cro.rendimento_qtd = 50
    db.session.add(ReceitaIngrediente(
        receita_id=cro.id, tipo='receita', sub_receita_id=massa.id,
        ingrediente_nome='Massa para folhar', porcentagem=1))
    # massa JÁ BATIDA: 5 un cobrem as 2 que 100 croissants pedem
    db.session.add(EstoqueProducao(receita_id=massa.id, quantidade=5))
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=3), cro, 100)

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rm = _rec_out(crono, massa.id)
    assert rm is not None and rm['insumo']
    assert rm['em_estoque'] == 5                        # estoque REAL, não 0
    assert rm['total'] == 0                             # 5 cobrem as 2 → nada a produzir
    assert all(c['qtd'] == 0 for c in rm['por_dia'])
    assert rm['breakdown_bom'][0]['qtd'] == 2           # o BOM continua puxando 2


def test_insumo_sem_estoque_produz_e_mostra_zero(app):
    """Contraprova: mesma massa SEM estoque batido → produz as 2 un e mostra
    em_estoque 0 (aí o 0 é real)."""
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
    assert rm is not None
    assert rm['em_estoque'] == 0                        # 0 real (nada batido)
    assert rm['total'] == 2                             # precisa produzir as 2


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
    """A rota /telaindustriateste/previsao/<id> renderiza a decomposição.

    O seed é histórico de PEDIDOS, então pede ?motor=pedidos explícito —
    desde 17/08/2026 o default da tela é 'vendas' e sem o param a
    decomposição por loja sairia vazia (não é o que este teste cobre)."""
    loja = _loja('Anesio')
    r = _receita('Croissant')
    db.session.commit()
    alvo = hoje() + timedelta(days=2)
    for semanas in range(1, 4):
        _pedido(loja, 'entregue', alvo - timedelta(days=7 * semanas), r, 250)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/telaindustriateste/previsao/%d?motor=pedidos' % r.id)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'De onde vem a previsão' in html
    assert 'Anesio' in html


def test_rota_telaindustriateste(app, admin_user):
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
    loja = _loja()
    r = _receita()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 10)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/telaindustriateste/')
    assert resp.status_code == 200
    assert 'cronograma' in resp.get_data(as_text=True).lower()


def test_rota_telaindustriateste_renderiza_aviso_stale(app, admin_user):
    """E3: a página renderiza o aviso de edição desatualizada (template válido
    com override_stale)."""
    from datetime import datetime

    from app.models import CronogramaOverride
    loja = _loja()
    r = _receita()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 10)
    # override antigo e divergente do cálculo -> stale.
    ontem = hoje() - timedelta(days=2)
    db.session.add(CronogramaOverride(
        receita_id=r.id, data=hoje(), qtd=999,
        criado_em=datetime(ontem.year, ontem.month, ontem.day, 12, 0)))
    db.session.commit()

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/telaindustriateste/')
    assert resp.status_code == 200
    assert 'pode estar desatualizada' in resp.get_data(as_text=True)


def test_rota_renderiza_rastreabilidade_do_insumo(app, admin_user):
    """A tela renderiza o expandir do insumo com a origem (breakdown_bom) e a
    coluna Previsto — pro padeiro ver de onde sai a quantidade de massa."""
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
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
    # equilibrar=0 explícito: o teste valida a mecânica do aprovar no DIA da
    # demanda (modo curva); no default nivelado a produção migraria pra
    # segunda e o dia alvo sairia vazio.
    resp = client.post('/telaindustriateste/aprovar',
                       data={'data': d2.isoformat(), 'horizonte': 7,
                             'janela': 6, 'equilibrar': 0})
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
                        'inicio': 0, 'equilibrar': 0})   # modo curva explícito
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


def test_lote_producao_arredonda_pra_cima_pedido_livre(app):
    """Focaccia: placa de 8 pedaços. Lojas pedem LIVRE (11 pedaços); a produção
    arredonda PRA CIMA (16 = 2 placas — nunca falta; a sobra fica na indústria).
    lote_producao NÃO mexe no pedido de loja (só lote_pedido faz isso)."""
    from datetime import timedelta as td
    loja = _loja()
    r = _receita('Focaccia Gorgonzola')
    r.lote_producao = 8                          # placa; lote_pedido fica NULL
    db.session.commit()
    d3 = hoje() + td(days=3)
    _pedido(loja, 'pendente', d3, r, 11)         # pedido livre: 11 pedaços

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rc = _rec_out(crono, r.id)
    assert rc is not None
    assert rc['total'] == 16                     # ceil(11/8) = 2 placas
    assert all(q % 8 == 0 for q in
               (c['qtd'] for c in rc['por_dia']))  # só placas inteiras


def test_lote_producao_nao_forca_pedido_da_loja(app):
    """A tela de pedidos por venda+estoque NÃO usa lote_producao — a sugestão
    de pedido da loja sai livre (7), sem arredondar pra placa."""
    from datetime import datetime as dt
    from datetime import time as tm
    from datetime import timedelta as td

    from app.models import EstoqueLoja, MovEstoqueLoja
    from app.services.previsao_producao import sugerir_pedidos_por_venda
    loja = _loja()
    r = _receita('Focaccia Gorgonzola')
    r.lote_producao = 8
    db.session.commit()
    el = EstoqueLoja(loja_id=loja.id, receita_id=r.id, quantidade=0)
    db.session.add(el)
    db.session.flush()
    alvo = hoje()
    while alvo.weekday() != 0:
        alvo += td(days=1)
    for sem in range(1, 7):
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo='venda_seru', quantidade=7,
            data=dt.combine(alvo - td(days=7 * sem), tm(12, 0)),
            referencia='t'))
    db.session.commit()

    grade = sugerir_pedidos_por_venda(horizonte_dias=7, janela_semanas=6,
                                      inicio_offset_dias=(alvo - hoje()).days)
    lj = next(e for e in grade['lojas'] if e['loja_id'] == loja.id)
    p = next(x for x in lj['produtos'] if x['receita_id'] == r.id)
    assert p['lote'] == 0                        # sem caixa de PEDIDO
    assert p['por_dia'][0] == 7                  # sugestão livre, não 8


def test_lote_pedido_sem_lote_producao_mantem_arredondamento_antigo(app):
    """Sem lote_producao, o fallback é o lote_pedido com o arredondamento
    original (mais próximo — decisão 29/06): croissant cx 50, demanda 100
    → produz 100 (2 caixas), não muda nada."""
    from datetime import timedelta as td
    loja = _loja()
    r = _receita('Croissant Tradicional')
    r.lote_pedido = 50
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + td(days=3), r, 100)

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rc = _rec_out(crono, r.id)
    assert rc['total'] == 100                    # comportamento preservado


def test_editar_celula_com_pendencia_reagendada(app):
    """Regressão do incidente de 02/07 ('não consigo editar a produção de
    hoje'): receita com pendência REAGENDADA no plano de hoje (qtd_extra > 0,
    fluxo da auditoria) tem que continuar editável no cronograma — a pendência
    não pode tirar a receita/dia do recompute do editar_celula (404)."""
    from app.models import PlanejamentoItem, PlanejamentoProducao
    from app.services.cronograma_edit import editar_celula

    loja = _loja()
    r = _receita('Sourdough 7 Grãos')
    _pedido(loja, 'pendente', hoje() + timedelta(days=2), r, 50)
    plano = PlanejamentoProducao(data=hoje(), nome='Plano hoje',
                                 status='aprovado', origem='cronograma',
                                 enviado_ao_padeiro=True)
    db.session.add(plano)
    db.session.flush()
    db.session.add(PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                                    multiplicador=1, qtd_alvo=20,
                                    produzido_qtd=0, qtd_extra=20))
    db.session.commit()

    res = editar_celula(r.id, hoje().isoformat(), 30, horizonte_dias=7,
                        janela_semanas=6, inicio_offset_dias=0)
    assert res is not None
    assert res['por_dia'][0]['qtd'] == 30


def test_rota_celula_com_pendencia_reagendada(app, admin_user):
    """Mesma regressão pela rota: POST /telaindustriateste/celula com plano de
    hoje carregando reagendados (qtd_extra) devolve 200 ok."""
    from app.models import PlanejamentoItem, PlanejamentoProducao

    loja = _loja('Loja B')
    r = _receita('Sourdough Integral')
    _pedido(loja, 'pendente', hoje() + timedelta(days=2), r, 50)
    plano = PlanejamentoProducao(data=hoje(), nome='Plano hoje',
                                 status='aprovado', origem='cronograma',
                                 enviado_ao_padeiro=True)
    db.session.add(plano)
    db.session.flush()
    db.session.add(PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                                    multiplicador=1, qtd_alvo=20,
                                    produzido_qtd=0, qtd_extra=20))
    db.session.commit()

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'})
    resp = client.post('/telaindustriateste/celula', json={
        'receita_id': r.id, 'data': hoje().isoformat(), 'qtd': 30,
        'horizonte': 7, 'janela': 6, 'inicio': 0, 'equilibrar': 0})
    assert resp.status_code == 200
    assert resp.get_json()['ok'] is True


# ── Alerta "pedido programado sem produto" (entregas_risco / alertas_falta) ──

def test_alerta_entrega_firme_descoberta(app):
    """Lead 2 + entrega firme AMANHÃ sem estoque: a produção só fica pronta em
    hoje+2, então a entrega de amanhã não tem produto mesmo produzindo como
    programado → entra em entregas_risco e no agregado alertas_falta."""
    loja = _loja()
    r = _receita()
    r.dias_producao = 2
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 30)

    crono = cronograma_producao(horizonte_dias=7)
    rr = _rec_out(crono, r.id)
    assert rr['entregas_risco'], 'entrega descoberta tinha que alertar'
    e = rr['entregas_risco'][0]
    assert e['data'] == (hoje() + timedelta(days=1)).isoformat()
    assert e['firme'] == 30
    assert e['faltam'] == 30
    assert rr['risco_datas'] == [e['data']]
    alerta = next((a for a in crono['alertas_falta']
                   if a['receita_id'] == r.id), None)
    assert alerta is not None
    assert alerta['entregas'][0]['faltam'] == 30


def test_alerta_nao_dispara_com_estoque_suficiente(app):
    """Mesmo cenário com estoque cobrindo a entrega: sem alerta."""
    loja = _loja()
    r = _receita()
    r.dias_producao = 2
    db.session.add(EstoqueProducao(receita_id=r.id, quantidade=50))
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 30)

    crono = cronograma_producao(horizonte_dias=7)
    rr = _rec_out(crono, r.id)
    assert rr['entregas_risco'] == []
    assert all(a['receita_id'] != r.id for a in crono['alertas_falta'])


def test_alerta_nao_dispara_quando_producao_chega_a_tempo(app):
    """Entrega em hoje+2 com lead 2: produz hoje, fica pronta no dia da
    entrega → coberta, sem alerta."""
    loja = _loja()
    r = _receita()
    r.dias_producao = 2
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=2), r, 30)

    crono = cronograma_producao(horizonte_dias=7)
    rr = _rec_out(crono, r.id)
    assert rr['por_dia'][0]['qtd'] == 30    # produz hoje
    assert rr['entregas_risco'] == []


def test_alerta_ignora_falta_so_de_previsao(app):
    """Falta contra o PREVISTO (histórico) sem pedido firme não acende o
    alerta — ele é sobre pedido real programado. A projeção detalhada da tela
    continua mostrando a falta prevista (dia_falta)."""
    loja = _loja()
    r = _receita()
    db.session.commit()
    # Histórico forte no MESMO dia-da-semana de amanhã, nas últimas 3 semanas
    # → previsto alto pra amanhã; nenhum pedido firme na janela.
    alvo = hoje() + timedelta(days=1)
    for semanas in range(1, 4):
        _pedido(loja, 'entregue', alvo - timedelta(days=7 * semanas), r, 40)

    crono = cronograma_producao(horizonte_dias=7)
    rr = _rec_out(crono, r.id)
    assert rr['previsto'] > 0               # a previsão existe...
    assert rr['entregas_risco'] == []       # ...mas não é pedido firme
    assert crono['alertas_falta'] == []


def test_alerta_celula_editada_pra_baixo_descobre_entrega(app):
    """Entrega coberta pelo cronograma, mas o admin EDITA a célula pra 0
    (override): a entrega fica descoberta e o alerta acende — é o caso 'eu
    mexi na grade e nem vi que furou a entrega'."""
    from app.services.cronograma_edit import editar_celula

    loja = _loja()
    r = _receita()
    r.dias_producao = 2
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=2), r, 30)

    editar_celula(r.id, hoje().isoformat(), 0, horizonte_dias=7)
    crono = cronograma_producao(horizonte_dias=7)
    rr = _rec_out(crono, r.id)
    assert rr['por_dia'][0]['qtd'] == 0     # override aplicado
    assert rr['entregas_risco']
    assert rr['entregas_risco'][0]['data'] == \
        (hoje() + timedelta(days=2)).isoformat()


def test_rota_renderiza_banner_entregas_risco(app, admin_user):
    """A página mostra o banner com a receita e o realce da célula."""
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
    loja = _loja()
    r = _receita('Sourdough Nozes')
    r.dias_producao = 2
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 30)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/telaindustriateste/')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'Entregas em risco' in html
    assert 'Sourdough Nozes' in html
    assert 'cel-risco' in html


def test_rota_sem_risco_nao_mostra_banner(app, admin_user):
    loja = _loja()
    r = _receita()
    _pedido(loja, 'pendente', hoje() + timedelta(days=3), r, 30)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/telaindustriateste/')
    assert resp.status_code == 200
    assert 'Entregas em risco' not in resp.get_data(as_text=True)


# ── upgrade da tela (06/07/2026): resumo, filtros e totais por dia ──────────

def test_rota_renderiza_resumo_filtros_e_totais(app, admin_user):
    """A tela traz a faixa de resumo (KPIs), a toolbar de filtros, o rodapé
    'Total do dia' e o explicador colapsável — sem perder o grid."""
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
    loja = _loja()
    r = _receita()
    _pedido(loja, 'pendente', hoje() + timedelta(days=2), r, 50)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/telaindustriateste/')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'kpi-strip' in html
    assert 'aguardando confirmação do padeiro' in html
    assert 'ocultar zerados' in html
    assert 'crono-busca' in html
    assert 'Total do dia' in html
    assert 'Como funciona esta tela' in html


def test_rota_totais_do_dia_excluem_insumo(app, admin_user):
    """O rodapé soma unidades de PRODUTO FINAL — a massa (insumo, em bolas)
    fica fora da soma de unidades (senão bolas somavam com croissants)."""
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
    from app.models import ReceitaIngrediente

    loja = _loja()
    massa = _receita('Massa para folhar')
    cro = _receita('Croissant')
    db.session.add(ReceitaIngrediente(
        receita_id=cro.id, tipo='receita', sub_receita_id=massa.id,
        ingrediente_nome=massa.nome, porcentagem=1))
    db.session.commit()
    _pedido(loja, 'pendente', hoje() + timedelta(days=2), cro, 100)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    # equilibrar=0: no modo nivelado os 100 se fatiam em lotes por dia — o
    # teste é sobre o RODAPÉ (insumo fora da soma), então usa a curva.
    html = client.get('/telaindustriateste/?equilibrar=0').get_data(
        as_text=True)
    # dia da produção do croissant: 100 un no rodapé (a massa não soma)
    assert '<span class="tot-dia-un">100 un</span>' in html


def test_rota_header_do_dia_com_menu(app, admin_user):
    """Cabeçalho do dia: ação primária visível (📤 enviar) + menu ⋯ com as
    secundárias — antes eram 2-3 botões empilhados repetidos por coluna."""
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
    loja = _loja()
    r = _receita()
    _pedido(loja, 'pendente', hoje() + timedelta(days=2), r, 50)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    html = client.get('/telaindustriateste/').get_data(as_text=True)
    assert 'btn-dia-menu' in html
    assert '📤 enviar' in html                      # primária visível
    assert 'só aprovar (rascunho pra revisar)' in html   # secundária no menu


def test_rota_header_dia_enviado_menu_completo(app, admin_user):
    """Dia ENVIADO: badge visível; atualizar/editar/excluir vivem no menu ⋯
    (o 'atualizar produção' explícito continua existindo — garantia do dono)."""
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
    from app.services.producao import aprovar_plano_do_dia, enviar_plano_do_dia

    loja = _loja()
    r = _receita()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 30)
    aprovar_plano_do_dia(d2, admin_user.id, horizonte_dias=7)
    enviar_plano_do_dia(d2)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    # equilibrar=0: a ordem foi criada no modo curva (service default) — a
    # tela na MESMA visão mostra "enviado" limpo, sem o badge de divergência.
    html = client.get('/telaindustriateste/?equilibrar=0').get_data(
        as_text=True)
    assert '📤 enviado' in html
    assert 'atualizar produção (aplica o grid)' in html
    assert 'excluir ordem' in html
    assert 'editar a ordem' in html


def test_rota_renderiza_badge_capado_ao_retorno(app, admin_user):
    """Receita capada pela política 'só de sobras' mostra o badge ♻️ com o
    porquê (antes o cap era invisível na tela — só no expandir)."""
    app.config['UI_V2_ENABLED'] = False  # contrato da tela CLASSICA (viva via cookie ui_classic/?legacy=1)
    from app.models import ReceitaIngrediente

    retorno = _receita('Croissant Tradicional — Retorno')
    trad = _receita('Croissant Tradicional')
    trad.retorno_receita_id = retorno.id
    almond = _receita('Croissant Almond')
    db.session.add(ReceitaIngrediente(
        receita_id=almond.id, tipo='receita', sub_receita_id=retorno.id,
        ingrediente_nome=retorno.nome, porcentagem=1))
    db.session.add(EstoqueProducao(receita_id=retorno.id, quantidade=15))
    db.session.commit()
    loja = _loja()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), almond, 40)

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    html = client.get('/telaindustriateste/').get_data(as_text=True)
    assert 'capado ao retorno' in html
    assert 'Croissant Tradicional — Retorno' in html


# ---------------------------------------------------------------------------
# Interface v2 do planejamento da indústria (promovida do preview 18/08/2026)
# ---------------------------------------------------------------------------

def test_rota_telaindustriateste_v2_funcional(app, admin_user):
    """A tela nova usa os MESMOS dados e expõe a grade editável sem
    substituir a tela operacional (que segue no default sem a flag)."""
    loja = _loja()
    r = _receita()
    _pedido(loja, 'pendente', hoje() + timedelta(days=1), r, 10)
    from app.models import PlanejamentoProducao
    db.session.add(PlanejamentoProducao(
        data=hoje(), nome='Plano hoje', status='aprovado',
        origem='cronograma', enviado_ao_padeiro=True))
    db.session.commit()

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)

    nova = client.get('/telaindustriateste/?v2=1')
    assert nova.status_code == 200
    html = nova.get_data(as_text=True)
    assert 'Planejamento automático · produção' in html
    assert 'somente leitura' not in html
    assert 'Motor de previsão' in html
    assert 'Motor funcionando normalmente' in html
    assert 'Cálculo concluído' in html
    assert 'Ordens de produção' in html
    assert 'Ordem enviada' in html
    assert 'Atualização automática é o comportamento esperado' not in html
    assert 'class="load-track"' not in html
    assert 'Revisar alterações' not in html
    assert 'aguardando confirmação' not in html
    assert 'Planejamento semanal' in html
    assert r.nome in html
    assert 'id="week-grid"' in html
    assert 'class="plan-input' in html
    assert '/telaindustriateste/celula' in html
    assert 'Enviar' in html

    # v2 e o DEFAULT: a rota crua tambem rende a tela nova; a antiga
    # continua acessivel por ?legacy=1
    atual = client.get('/telaindustriateste/')
    assert 'Planejamento automático · produção' in atual.get_data(
        as_text=True)
    antiga = client.get('/telaindustriateste/?legacy=1')
    assert 'Planejamento automático · produção' not in antiga.get_data(
        as_text=True)


def test_v2_deixa_claro_o_numero_ja_enviado_ao_padeiro(app, admin_user):
    """A sugestão zero não pode parecer ausência da ordem ativa.

    Caso real do Pão Francês em 24/08: o grid recalculou zero para hoje, mas a
    ordem do padeiro continuava em 300. O texto explícito separa as duas coisas.
    """
    from app.models import PlanejamentoItem, PlanejamentoProducao

    r = _receita('Pão Francês Fermentado')
    plano = PlanejamentoProducao(
        data=hoje(), nome='Plano hoje', status='aprovado',
        origem='cronograma', enviado_ao_padeiro=True)
    db.session.add(plano)
    db.session.flush()
    db.session.add(PlanejamentoItem(
        planejamento_id=plano.id, receita_id=r.id, multiplicador=2,
        qtd_alvo=300, produzido_qtd=0))
    db.session.commit()

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    html = client.get('/telaindustriateste/?v2=1').get_data(as_text=True)

    assert 'Ordem enviada: 300' in html
    assert 'Atualizar ordem' in html
    assert '>Alterado<' not in html


def test_flag_torna_nova_industria_padrao_com_comparacao_legacy(
        app, admin_user):
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    app.config['UI_V2_ENABLED'] = True

    nova = client.get('/telaindustriateste/').get_data(as_text=True)
    antiga = client.get('/telaindustriateste/?legacy=1').get_data(as_text=True)

    assert 'Planejamento automático · produção' in nova
    assert 'Comparar com tela antiga' in nova
    assert 'Planejamento automático · produção' not in antiga


def test_v2_monitora_parametros_reais_do_motor(app, admin_user):
    """A visão geral descreve o cálculo que acabou de acontecer nesta
    abertura; não inventa cron/horário futuro e não chama recálculo de tarefa."""
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)

    html = client.get(
        '/telaindustriateste/?motor=pedidos&horizonte=5&equilibrar=0'
    ).get_data(as_text=True)

    assert 'Pedidos das lojas' in html
    assert '<dd>5 dias</dd>' in html
    assert 'Distribuição pela demanda' in html
    assert 'Previsão recalculada nesta abertura às' in html
    assert 'Próxima execução' not in html
    assert 'Prioridades' not in html


def test_v2_preserva_grade_apos_post(app, admin_user):
    """Ações da grade voltam pra própria grade nova — editar/limpar não
    pode devolver o usuário à tela antiga."""
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)

    resp = client.post('/telaindustriateste/limpar-edicoes', data={
        'horizonte': 7, 'janela': 6, 'inicio': 0, 'motor': 'vendas',
        'equilibrar': 1, 'v2': 1, 'view': 'week',
    })

    assert resp.status_code in (302, 303)
    assert 'v2=1' in resp.location
    assert 'view=week' in resp.location


def test_antecedencia_por_receita_zero_nao_antecipa(app):
    """Caso Brioche fresco (dono 18/08/2026: "quero o máximo de brioche
    fresco nas lojas"): receita com antecedencia_max_dias=0 NUNCA é
    antecipada pelo nivelador — cada dia assa só a demanda do próprio dia
    (fim de semana segue caindo na sexta pela rolagem do calendário)."""
    from datetime import date as _date

    loja = _loja()
    r = _receita('Brioche Fresquinho')
    r.lote_producao = 10
    r.antecedencia_max_dias = 0
    db.session.commit()
    hoje_d = hoje()                                # segunda congelada
    demanda = {}
    for i in range(1, 7):                          # ter..dom, 20/dia
        d = hoje_d + timedelta(days=i)
        _pedido(loja, 'pendente', d, r, 20)
        demanda[d.isoformat()] = 20

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0,
                                equilibrar=True)
    rr = _rec_out(crono, r.id)
    assert rr is not None and rr['total'] >= 100
    for c in rr['por_dia']:
        if not c['qtd']:
            continue
        d = _date.fromisoformat(c['data'])
        if d.weekday() == 4:                       # sexta = própria + fds
            assert c['qtd'] >= demanda.get(c['data'], 0)
        else:
            # nada produzido ANTES do dia da demanda: célula == demanda
            assert c['qtd'] == demanda.get(c['data'], 0), (c['data'], c['qtd'])


def test_antecedencia_por_receita_null_usa_global(app):
    """Sem valor no cadastro (NULL), vale a regra global de 3 dias — a
    parcela de sexta ainda pode ser antecipada até terça."""
    loja = _loja()
    r = _receita('Pão Regra Global')
    db.session.commit()
    hoje_d = hoje()
    _pedido(loja, 'pendente', hoje_d + timedelta(days=4), r, 90)   # sex

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0,
                                equilibrar=True)
    rr = _rec_out(crono, r.id)
    assert rr is not None
    dias_com_prod = [c['data'] for c in rr['por_dia'] if c['qtd']]
    # o nivelador PODE espalhar pra antes de sexta (ter..qui) — se tudo
    # ficou só na sexta, a regra global não está valendo
    assert any(_d < (hoje_d + timedelta(days=4)).isoformat()
               for _d in dias_com_prod), dias_com_prod


def _vendas_diarias(loja, receita, un_dia=20, semanas=4):
    """Historico de venda POR DIA (motor 'vendas'): e o vetor que produz
    celulas sub-lote em dias seguidos no grid real (caso Sourdough
    Integral 98+22 do dono, 19/08/2026)."""
    from datetime import datetime as _dt
    from datetime import time as _time

    from app.models import EstoqueLoja, MovEstoqueLoja
    el = EstoqueLoja(loja_id=loja.id, receita_id=receita.id, quantidade=0)
    db.session.add(el)
    db.session.flush()
    hoje_d = hoje()
    for d in range(1, 7 * semanas + 1):
        dia = hoje_d - timedelta(days=d)
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo='venda_seru', quantidade=un_dia,
            data=_dt.combine(dia, _time(12, 0)), referencia='teste-refornada'))
    db.session.commit()


def test_anti_refornada_funde_topup_na_fornada_anterior(app):
    """Caso Sourdough Integral (dono 19/08/2026: "se vai produzir hoje,
    amanhã não deveria produzir novamente — é um re-trabalho"): no motor
    de VENDAS (o do grid real), item congelado com lote definido não
    deixa top-up MENOR QUE UM LOTE colado num dia que já produz — a
    célula quebrada é fundida na fornada anterior."""
    loja = _loja()
    r = _receita('Sourdough Consolida')
    r.lote_producao = 60
    db.session.commit()
    _vendas_diarias(loja, r)

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0,
                                equilibrar=True, motor='vendas')
    rr = _rec_out(crono, r.id)
    assert rr is not None and rr['total'] > 0
    qtds = [c['qtd'] for c in rr['por_dia']]
    for i in range(1, len(qtds)):
        if qtds[i - 1] > 0 and qtds[i] > 0:
            assert qtds[i] >= 60, (
                'top-up sub-lote no dia seguinte a uma fornada: %s' % qtds)


def test_anti_refornada_respeita_item_fresco(app):
    """Brioche (antecedência 0, não congela) fica FORA da consolidação:
    as fornadas diárias pequenas continuam — a parcela de amanhã não
    pode ser assada hoje. Mesmo vetor de vendas do teste acima."""
    loja = _loja()
    r = _receita('Brioche Diario')
    r.lote_producao = 60
    r.antecedencia_max_dias = 0
    db.session.commit()
    _vendas_diarias(loja, r)

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0,
                                equilibrar=True, motor='vendas')
    rr = _rec_out(crono, r.id)
    assert rr is not None and rr['total'] > 0
    dias_com_prod = [c['qtd'] for c in rr['por_dia'] if c['qtd']]
    # produção espalhada em VÁRIOS dias preservada — o congelado do teste
    # acima colapsa em menos dias; o fresco não pode ser fundido (o
    # arredondamento por lote pré-existente pode encher as células, mas
    # a CONTAGEM de dias de fornada é o contrato do fresco)
    assert len(dias_com_prod) >= 3, dias_com_prod


def test_teto_diario_corta_so_previsao_automatica(app):
    """Brioche com giro historico alto nunca recebe sugestao automatica
    acima de 40 em um dia, inclusive depois do nivelamento/rolagem do fim de
    semana. O total pode cair: teto de capacidade e uma restricao real."""
    loja = _loja()
    r = _receita('Brioche')
    r.lote_producao = 10
    r.antecedencia_max_dias = 0
    r.producao_max_dia = 40
    db.session.commit()
    _vendas_diarias(loja, r, un_dia=100, semanas=4)

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0,
                                equilibrar=True, motor='vendas')
    rr = _rec_out(crono, r.id)
    assert rr is not None and rr['total'] > 0
    assert max(c['qtd'] for c in rr['por_dia']) == 40
    assert all(c['qtd'] <= 40 for c in rr['por_dia'])
    assert rr['limitado_teto'] is True
    assert rr['producao_max_dia'] == 40


def test_teto_diario_mantem_pedido_firme_como_risco(app):
    """Encomenda acima da capacidade nao some: o plano para em 40 e a
    diferenca fica explicitamente marcada como entrega em risco."""
    loja = _loja()
    r = _receita('Brioche')
    r.producao_max_dia = 40
    r.antecedencia_max_dias = 0
    db.session.commit()
    d2 = hoje() + timedelta(days=2)
    _pedido(loja, 'pendente', d2, r, 75)

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0,
                                equilibrar=True)
    rr = _rec_out(crono, r.id)
    cel = next(c for c in rr['por_dia'] if c['data'] == d2.isoformat())
    assert cel['qtd'] == 40
    assert rr['total'] == 40
    risco = next(e for e in rr['entregas_risco']
                 if e['data'] == d2.isoformat())
    assert risco['firme'] == 75
    assert risco['faltam'] == 35


def test_teto_diario_nao_corta_edicao_manual(app):
    """Ajuste consciente do administrador fica acima de 40: override roda
    depois da protecao automatica."""
    from app.models import CronogramaOverride

    r = _receita('Brioche')
    r.producao_max_dia = 40
    db.session.add(CronogramaOverride(
        receita_id=r.id, data=hoje() + timedelta(days=1), qtd=70))
    db.session.commit()

    crono = cronograma_producao(horizonte_dias=7, inicio_offset_dias=0)
    rr = _rec_out(crono, r.id)
    cel = next(c for c in rr['por_dia']
               if c['data'] == (hoje() + timedelta(days=1)).isoformat())
    assert cel['qtd'] == 70
    assert rr['total'] == 70


def test_piso_sourdough_produz_200_por_dia_sem_demanda(app):
    """O piso forma estoque e e dividido entre os paes finais. Preparos
    auxiliares e Brioche nao inflam artificialmente as 200 unidades."""
    app.config['SOURDOUGH_MIN_DIA'] = 200

    tradicional = _receita('Sourdough Tradicional')
    tradicional.familia = 'pao_sourdough'
    graos = _receita('Sourdough 7 Grãos')
    graos.familia = 'pao_sourdough'
    granola = _receita('Produção - Granola Artesanal 1000g')
    granola.categoria = 'Granola'
    levain = _receita('Levain')
    iogurte = _receita('Iogurte Natural')
    iogurte.categoria = 'Iogurte'
    brioche = _receita('Brioche')
    brioche.familia = 'viennoiserie'
    db.session.commit()

    crono = cronograma_producao(horizonte_dias=7, equilibrar=True)
    paes = [_rec_out(crono, tradicional.id), _rec_out(crono, graos.id)]
    assert all(paes)

    # A semana congelada deste arquivo começa na segunda: seg-sex recebem
    # 200 por dia e sab/dom continuam bloqueados.
    for i in range(5):
        assert sum(rr['por_dia'][i]['qtd'] for rr in paes) == 200
    for i in (5, 6):
        assert sum(rr['por_dia'][i]['qtd'] for rr in paes) == 0

    # Sem sinal de giro, o fallback e equilibrado: nao joga tudo em um unico
    # pao (o caso operacional que motivou a mudanca).
    assert [rr['por_dia'][0]['qtd'] for rr in paes] == [100, 100]
    assert sum(rr['total'] for rr in paes) == 1000

    for rec in (granola, levain, iogurte, brioche):
        rr = _rec_out(crono, rec.id)
        assert rr is None or rr['total'] == 0


def _historico_mesmo_dia(loja, receita, alvo, venda, merma=0):
    """Cria seis semanas estáveis para um único dia da semana."""
    from datetime import datetime as _dt
    from datetime import time as _time

    from app.models import EstoqueLoja, MovEstoqueLoja
    el = EstoqueLoja.query.filter_by(
        loja_id=loja.id, receita_id=receita.id).first()
    if el is None:
        el = EstoqueLoja(
            loja_id=loja.id, receita_id=receita.id, quantidade=0)
        db.session.add(el)
        db.session.flush()
    for sem in range(1, 7):
        momento = _dt.combine(
            alvo - timedelta(days=7 * sem), _time(12, 0))
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo='venda_seru',
            quantidade=venda, data=momento, referencia='teste-venda-dia'))
        if merma:
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=el.id, tipo='perda',
                quantidade=merma, data=momento, referencia='teste-merma-dia'))
    db.session.commit()
    return el


def test_reposicao_por_venda_diaria_ignora_estoque_merma_e_caixa(app):
    """Nebraska/Croissant fresco recebe o que vende no dia, não caixa de 250."""
    from app.services.previsao_producao import sugerir_pedidos_por_venda

    loja = _loja('Loja Nebraska')
    receita = _receita('Croissant Tradicional')
    receita.lote_pedido = 250
    receita.minimo_pedido = 250
    alvo = hoje()
    el = _historico_mesmo_dia(
        loja, receita, alvo, venda=17, merma=80)
    el.quantidade = 375
    el.reposicao_por_venda_diaria = True
    db.session.commit()

    grade = sugerir_pedidos_por_venda(
        horizonte_dias=1, janela_semanas=6,
        inicio_offset_dias=(alvo - hoje()).days)
    lj = next(x for x in grade['lojas'] if x['loja_id'] == loja.id)
    produto = next(x for x in lj['produtos']
                   if x['receita_id'] == receita.id)

    assert produto['por_dia'] == [17]
    assert produto['reposicao_por_venda_diaria'] is True
    assert produto['lote'] == 250  # cadastro global continua intacto


def test_reposicao_padrao_continua_usando_estoque_merma_e_caixa(app):
    """Sem o modo por loja, a regra global antiga permanece inalterada."""
    from app.services.previsao_producao import sugerir_pedidos_por_venda

    loja = _loja('Outra loja')
    receita = _receita('Croissant Tradicional')
    receita.lote_pedido = 250
    receita.minimo_pedido = 250
    alvo = hoje()
    _historico_mesmo_dia(loja, receita, alvo, venda=17)

    grade = sugerir_pedidos_por_venda(
        horizonte_dias=1, janela_semanas=6,
        inicio_offset_dias=(alvo - hoje()).days)
    lj = next(x for x in grade['lojas'] if x['loja_id'] == loja.id)
    produto = next(x for x in lj['produtos']
                   if x['receita_id'] == receita.id)

    assert produto['por_dia'] == [250]
    assert produto['reposicao_por_venda_diaria'] is False


def test_seed_regras_reposicao_configura_somente_loja_produto_alvo(app):
    """O deploy aplica Choconana/Ribeiro e Croissant/Nebraska uma única vez."""
    from app.migrations_legacy import _seed_regras_reposicao_lojas
    from app.models import AppConfig, EstoqueLoja

    ribeiro = _loja('Loja Ribeiro do Vale 455')
    nebraska = _loja('Loja Nebraska')
    outra = _loja('Loja Anésio')
    choconana = _receita('Choconana')
    croissant = _receita('Croissant Tradicional')
    db.session.add(EstoqueLoja(
        loja_id=outra.id, receita_id=croissant.id, quantidade=0))
    db.session.commit()

    _seed_regras_reposicao_lojas(app)

    el_choco = EstoqueLoja.query.filter_by(
        loja_id=ribeiro.id, receita_id=choconana.id).one()
    el_croissant = EstoqueLoja.query.filter_by(
        loja_id=nebraska.id, receita_id=croissant.id).one()
    el_outra = EstoqueLoja.query.filter_by(
        loja_id=outra.id, receita_id=croissant.id).one()
    assert el_choco.pedido_minimo_diario == 2
    assert el_croissant.reposicao_por_venda_diaria is True
    assert el_outra.reposicao_por_venda_diaria is False
    assert AppConfig.get('seed_regras_reposicao_lojas_2026_09')


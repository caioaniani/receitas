"""Gantt da produção: 1 amassadeira/forno e dois padeiros independentes."""
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
from app.services.gantt import PASSIVA_LONGA_MIN, montar_gantt


def _receita(nome, etapas, cap=50000, peso=1000.0):
    """etapas: lista de (nome, dur, equip, ativa)."""
    r = Receita(nome=nome, categoria='Pães', rendimento_qtd=10,
                rendimento_unidade='un', peso_base=peso,
                capacidade_amassadeira_g=cap)
    db.session.add(r)
    db.session.flush()
    # farinha 100% -> massa_receita_base = peso_base (necessário pra fornadas)
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                      ingrediente_nome='Farinha', porcentagem=100))
    for i, (n, d, eq, at) in enumerate(etapas):
        db.session.add(ReceitaEtapa(receita_id=r.id, ordem=i, nome=n,
                                    duracao_min=d, equipamento=eq, ativa=at))
    db.session.commit()
    return r


def _plano(dia, itens):
    """itens: lista de (receita, multiplicador, qtd_alvo, produzido)."""
    pl = PlanejamentoProducao(data=dia, origem='cronograma')
    db.session.add(pl)
    db.session.flush()
    for rec, mult, alvo, feito in itens:
        db.session.add(PlanejamentoItem(planejamento_id=pl.id, receita_id=rec.id,
                                        multiplicador=mult, qtd_alvo=alvo,
                                        produzido_qtd=feito))
    db.session.commit()
    return pl


def test_sem_plano_retorna_none(app):
    assert montar_gantt(date(2026, 7, 1)) is None


def test_agenda_etapas_em_ordem(app):
    dia = date(2026, 7, 1)
    r = _receita('Pão A', [
        ('Mise en place', 10, None, True),
        ('Amassamento', 15, 'amassadeira', True),
        ('Forno', 20, 'forno', True),
    ])
    _plano(dia, [(r, 1, 10, 0)])
    g = montar_gantt(dia)
    assert g is not None
    assert len(g['produtos']) == 1
    tar = g['produtos'][0]['tarefas']
    assert [t['etapa'] for t in tar] == ['Mise en place', 'Amassamento', 'Forno']
    # encadeadas: cada uma começa quando a anterior termina
    assert tar[0]['ini'] == 0 and tar[0]['fim'] == 10
    assert tar[1]['ini'] == 10 and tar[1]['fim'] == 25
    assert tar[2]['ini'] == 25 and tar[2]['fim'] == 45
    assert tar[0]['ini_hhmm'] == '06:00'
    assert tar[2]['fim_hhmm'] == '06:45'


def test_amassadeira_serializa_entre_receitas(app):
    """Duas receitas não podem amassar ao mesmo tempo (1 amassadeira)."""
    dia = date(2026, 7, 2)
    etapas = [('Amassamento', 30, 'amassadeira', True)]
    ra = _receita('Pão A', etapas)
    rb = _receita('Pão B', etapas)
    _plano(dia, [(ra, 1, 10, 0), (rb, 1, 10, 0)])
    g = montar_gantt(dia)
    janelas = []
    for p in g['produtos']:
        for t in p['tarefas']:
            if t['recurso'] == 'amassadeira':
                janelas.append((t['ini'], t['fim']))
    janelas.sort()
    assert len(janelas) == 2
    # sem sobreposição: o início do 2º >= fim do 1º
    assert janelas[1][0] >= janelas[0][1]


def test_mise_en_place_paralelo_ao_amassamento(app):
    """Enquanto a amassadeira trabalha sozinha, o padeiro adianta o mise en
    place de outra receita (o pedido explícito do dono)."""
    dia = date(2026, 7, 3)
    ra = _receita('Pão A', [
        ('Mise en place', 10, None, True),
        ('Amassamento', 60, 'amassadeira', True),
    ])
    rb = _receita('Pão B', [
        ('Mise en place', 10, None, True),
        ('Amassamento', 60, 'amassadeira', True),
    ])
    _plano(dia, [(ra, 1, 10, 0), (rb, 1, 10, 0)])
    g = montar_gantt(dia)
    # acha o amassamento de A e o mise en place de B
    tarefas = {(p['nome'], t['etapa']): t
               for p in g['produtos'] for t in p['tarefas']}
    am_a = tarefas[('Pão A', 'Amassamento')]
    mep_b = tarefas[('Pão B', 'Mise en place')]
    # o mise de B começa enquanto A amassa (paralelismo real)
    assert mep_b['ini'] < am_a['fim']
    assert mep_b['recurso'] == 'padeiro_paes'
    assert am_a['recurso'] == 'amassadeira'


def test_padeiros_de_paes_e_viennoiserie_trabalham_em_paralelo(app):
    """As duas pessoas podem iniciar trabalho manual ao mesmo tempo."""
    dia = date(2026, 7, 31)
    pao = _receita('Brioche', [('Modelar', 30, None, True)])
    viennoiserie = _receita('Croissant', [('Laminar', 30, None, True)])
    viennoiserie.categoria = 'Viennoiserie'
    viennoiserie.familia = 'viennoiserie'
    db.session.commit()
    _plano(dia, [(pao, 1, 10, 0), (viennoiserie, 1, 10, 0)])

    g = montar_gantt(dia)
    tarefas = {(p['nome'], t['etapa']): t
               for p in g['produtos'] for t in p['tarefas']}

    assert tarefas[('Brioche', 'Modelar')]['ini'] == 0
    assert tarefas[('Croissant', 'Laminar')]['ini'] == 0
    assert tarefas[('Brioche', 'Modelar')]['recurso'] == 'padeiro_paes'
    assert tarefas[('Croissant', 'Laminar')]['recurso'] == (
        'padeiro_viennoiserie')


def test_fermentacao_longa_vira_marcador(app):
    dia = date(2026, 7, 4)
    r = _receita('Pão Fermentação Natural', [
        ('Mise en place', 10, None, True),
        ('Amassamento', 15, 'amassadeira', True),
        ('Fermentação final (frio)', PASSIVA_LONGA_MIN + 100, 'camara_fria', False),
        ('Forno', 25, 'forno', True),
    ])
    _plano(dia, [(r, 1, 10, 0)])
    g = montar_gantt(dia)
    p = g['produtos'][0]
    # só as etapas até a fermentação longa entram no dia
    nomes = [t['etapa'] for t in p['tarefas']]
    assert 'Forno' not in nomes
    assert 'Fermentação final (frio)' not in nomes
    assert p['destino']           # marcador "→ câmara fria Xh"


def test_descanso_curto_fica_inline(app):
    """Descanso < 4h NÃO corta o dia — segue pro forno."""
    dia = date(2026, 7, 5)
    r = _receita('Pão de Mesa', [
        ('Amassamento', 15, 'amassadeira', True),
        ('Descanso', 90, None, False),     # < 4h
        ('Forno', 20, 'forno', True),
    ])
    _plano(dia, [(r, 1, 10, 0)])
    g = montar_gantt(dia)
    p = g['produtos'][0]
    nomes = [t['etapa'] for t in p['tarefas']]
    assert 'Forno' in nomes
    assert p['destino'] is None
    # o forno só começa depois do descanso de 90min
    forno = [t for t in p['tarefas'] if t['etapa'] == 'Forno'][0]
    assert forno['ini'] >= 15 + 90


def test_receita_sem_etapas_listada_a_parte(app):
    dia = date(2026, 7, 6)
    r = Receita(nome='Brigadeiro', categoria='Cremes', rendimento_qtd=10,
                rendimento_unidade='un', peso_base=1000.0)
    db.session.add(r)
    db.session.commit()
    _plano(dia, [(r, 1, 10, 0)])
    g = montar_gantt(dia)
    assert 'Brigadeiro' in g['sem_etapas']
    assert g['produtos'] == []


def test_item_ja_produzido_sai_do_gantt(app):
    dia = date(2026, 7, 7)
    r = _receita('Pão Pronto', [('Forno', 20, 'forno', True)])
    _plano(dia, [(r, 1, 10, 10)])    # alvo 10, já produziu 10 -> falta 0
    g = montar_gantt(dia)
    assert g['produtos'] == []


def _login(app, user):
    c = app.test_client()
    c.post('/auth/login', data={'login': user.login, 'senha': '123'},
           follow_redirects=True)
    return c


def test_layout_em_px_e_label_dentro(app):
    """As posições saem em px (escala fixa) e cada tarefa diz se o rótulo cabe
    dentro da barra (senão a barra fica só com o ícone)."""
    dia = date(2026, 7, 30)
    r = _receita('Pão X', [
        ('Mise en place', 5, None, True),                 # curta -> label fora
        ('Bulk + dobras (fermentação longa)', 120, None, True),  # longa -> cabe
    ])
    _plano(dia, [(r, 1, 10, 0)])
    g = montar_gantt(dia)
    assert g['canvas_px'] > 0
    tar = {t['etapa']: t for t in g['produtos'][0]['tarefas']}
    assert 'left_px' in tar['Mise en place'] and 'width_px' in tar['Mise en place']
    assert tar['Mise en place']['label_dentro'] is False           # 5min não cabe
    assert tar['Bulk + dobras (fermentação longa)']['label_dentro'] is True
    assert g['horas'][0]['px'] == 0


def test_rota_gantt_renderiza(app, admin_user):
    dia = date(2026, 7, 20)
    r = _receita('Pão Francês', [
        ('Mise en place', 10, None, True),
        ('Amassamento', 15, 'amassadeira', True),
        ('Forno', 25, 'forno', True),
    ])
    _plano(dia, [(r, 1, 10, 0)])
    c = _login(app, admin_user)
    resp = c.get('/padeiro/gantt?data=2026-07-20')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'Pão Francês' in html
    assert 'Fluxograma' in html
    assert 'Amassamento' in html
    # admin: nome do produto linka pro editor de etapas (acesso rápido)
    assert ('/receitas/%d/etapas' % r.id) in html


def test_gantt_produto_carrega_receita_id(app):
    dia = date(2026, 7, 22)
    r = _receita('Pão Z', [('Mise en place', 10, None, True),
                           ('Forno', 20, 'forno', True)])
    _plano(dia, [(r, 1, 10, 0)])
    g = montar_gantt(dia)
    prod = [p for p in g['produtos'] if p['nome'] == 'Pão Z'][0]
    assert prod['receita_id'] == r.id        # pro link "editar etapas" no fluxograma


def test_rota_gantt_sem_plano(app, admin_user):
    c = _login(app, admin_user)
    resp = c.get('/padeiro/gantt?data=2026-07-21')
    assert resp.status_code == 200
    assert 'Nenhum plano' in resp.get_data(as_text=True)


def test_fornadas_escalam_etapas_ativas(app):
    """3 fornadas: amassamento ocupa 3× o tempo base (1 batida por vez)."""
    dia = date(2026, 7, 8)
    # massa base = peso_base * 100% (só farinha) = 1000g; cap 1000 -> mult 3 = 3 fornadas
    r = _receita('Pão Grande', [('Amassamento', 10, 'amassadeira', True)],
                 cap=1000, peso=1000.0)
    _plano(dia, [(r, 3, 30, 0)])
    g = montar_gantt(dia)
    am = g['produtos'][0]['tarefas'][0]
    assert g['produtos'][0]['fornadas'] == 3
    assert am['dur'] == 30          # 10 base × 3 fornadas


# ── Massa-base no Gantt (modelo árvore): tronco + ramos ──────────────────────

def _rec_grupo(nome, agua, recheio=None):
    from app.models import ReceitaIngrediente
    r = Receita(nome=nome, categoria='Pães', rendimento_qtd=10,
                rendimento_unidade='un', peso_base=1000.0,
                capacidade_amassadeira_g=50000)
    db.session.add(r)
    db.session.flush()
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                      ingrediente_nome='Farinha', porcentagem=100))
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                      ingrediente_nome='Água', porcentagem=agua))
    if recheio:
        db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                          ingrediente_nome=recheio[0],
                                          porcentagem=recheio[1]))
    for i, (n, d, eq, at) in enumerate([
            ('Mise en place', 10, None, True),
            ('Amassamento', 15, 'amassadeira', True),
            ('Modelagem', 15, 'bancada', True),
            ('Forno', 20, 'forno', True)]):
        db.session.add(ReceitaEtapa(receita_id=r.id, ordem=i, nome=n,
                                    duracao_min=d, equipamento=eq, ativa=at))
    db.session.commit()
    return r


def _grupo_quatro(dia):
    pf = _rec_grupo('Pão Francês', 70)
    st = _rec_grupo('Sourdough', 80)
    s7 = _rec_grupo('7 Grãos', 80, recheio=('Grãos', 40))
    na = _rec_grupo('Nozes', 80, recheio=('Nozes', 75))
    mb = MassaBase(nome='Base')
    db.session.add(mb)
    db.session.flush()
    for i, r in enumerate([pf, st, s7, na]):
        db.session.add(MassaBaseItem(massa_base_id=mb.id, receita_id=r.id, ordem=i))
    db.session.commit()
    _plano(dia, [(pf, 1, 10, 0), (st, 1, 10, 0), (s7, 1, 10, 0), (na, 1, 10, 0)])
    return pf, st, s7, na, mb


def test_gantt_tronco_uma_amassada_e_retiradas(app):
    dia = date(2026, 9, 1)
    _grupo_quatro(dia)
    g = montar_gantt(dia)
    tronco = [p for p in g['produtos'] if p['tipo'] == 'base'][0]
    etapas = [t['etapa'] for t in tronco['tarefas']]
    # UMA amassada da base
    assert sum(1 for e in etapas if e.startswith('Amassar')) == 1
    # retira os 4 pães (lineares + ramos)
    for nome in ['Pão Francês', 'Sourdough', '7 Grãos', 'Nozes']:
        assert 'Tirar ' + nome in etapas
    # acréscimos: água (linha principal) e os recheios (ramos)
    assert any(e.startswith('+') and 'Água' in e for e in etapas)
    assert any(e.startswith('+') and 'Grãos' in e for e in etapas)
    assert any(e.startswith('+') and 'Nozes' in e for e in etapas)
    # a amassada usa a amassadeira; só ela
    amass = [t for t in tronco['tarefas'] if t['etapa'] == 'Amassar base']
    assert len(amass) == 1 and amass[0]['recurso'] == 'amassadeira'


def test_gantt_ramo_comeca_na_retirada(app):
    dia = date(2026, 9, 2)
    _grupo_quatro(dia)
    g = montar_gantt(dia)
    tronco = [p for p in g['produtos'] if p['tipo'] == 'base'][0]
    tirar = {t['etapa']: t for t in tronco['tarefas']}
    for nome in ['Pão Francês', '7 Grãos', 'Nozes']:
        ramo = [p for p in g['produtos']
                if p['tipo'] == 'ramo' and p['nome'] == nome][0]
        # o ramo não repete o amassamento e começa na retirada
        assert 'Amassamento' not in [t['etapa'] for t in ramo['tarefas']]
        assert ramo['tarefas'][0]['etapa'] == 'Modelagem'
        assert ramo['tarefas'][0]['ini'] >= tirar['Tirar ' + nome]['fim']


def test_gantt_tronco_mostra_qtd_e_receita_da_base(app):
    """O tronco carrega a quantidade da base e a receita ESCALADA pro plano."""
    dia = date(2026, 9, 3)
    pf, st, s7, na, mb = _grupo_quatro(dia)   # 1 porção de cada
    g = montar_gantt(dia)
    tronco = [p for p in g['produtos'] if p['tipo'] == 'base'][0]
    assert tronco['base_massa_label']                 # ex "5,76 kg"
    nomes = {ing['nome'] for ing in tronco['base_recipe']}
    assert 'Farinha' in nomes and 'Água' in nomes     # ingredientes da base
    # recheios NÃO entram na base (são dos ramos)
    assert 'Grãos' not in nomes and 'Nozes' not in nomes


def _sourdough_lead(nome, lead):
    """Pão de fermentação longa (24h) com etapa de assar depois — lead em dias."""
    r = _receita(nome, [
        ('Mise en place', 10, None, True),
        ('Amassamento', 20, 'amassadeira', True),
        ('Fermentação', 1440, 'camara_fria', False),   # ≥240 -> longa
        ('Assar', 40, 'forno', True),
    ])
    r.dias_producao = lead
    db.session.commit()
    return r


def test_continuacao_assar_aparece_no_dia_seguinte(app):
    """Pão amassado ONTEM (lead=1) aparece HOJE na FINALIZAÇÃO (assar) — o
    fluxograma é contínuo entre os dias, não some a parte de assar."""
    ontem, hoje_ = date(2026, 9, 10), date(2026, 9, 11)
    r = _sourdough_lead('Sourdough X', 1)
    _plano(ontem, [(r, 1, 10, 0)])              # amassado ontem
    _plano(hoje_, [])                           # hoje sem mistura nova

    g = montar_gantt(hoje_)
    assert g is not None
    cont = [p for p in g['produtos'] if p['tipo'] == 'continuacao']
    assert len(cont) == 1
    assert cont[0]['nome'] == 'Sourdough X'
    assert cont[0]['origem_label'] == ontem.strftime('%d/%m')
    etapas = [t['etapa'] for t in cont[0]['tarefas']]
    assert 'Assar' in etapas                    # a finalização (forno) é agendada
    assert 'Amassamento' not in etapas          # o amassamento NÃO se repete hoje


def test_continuacao_respeita_lead_de_2_dias(app):
    """lead=2 (48h): a finalização cai 2 dias após a amassada, não 1."""
    d0 = date(2026, 9, 15)
    r = _sourdough_lead('Sourdough 48h', 2)
    _plano(d0, [(r, 1, 10, 0)])
    # 1 dia depois: ainda fermentando, nada a finalizar
    assert montar_gantt(d0 + timedelta(days=1)) is None
    # 2 dias depois: finaliza
    g = montar_gantt(d0 + timedelta(days=2))
    assert g is not None
    assert any(p['tipo'] == 'continuacao' and p['nome'] == 'Sourdough 48h'
               for p in g['produtos'])


def test_dia_sem_plano_nem_continuacao_e_none(app):
    assert montar_gantt(date(2026, 9, 20)) is None


def test_tronco_retiradas_seguem_amassada_na_amassadeira(app):
    """As retiradas da massa-base encadeiam LOGO após o amassamento, ocupando a
    amassadeira (a base está nela) — não esperam o padeiro, que pode estar em
    outra receita. Sem gap entre 'Amassar base' e a 1ª retirada."""
    dia = date(2026, 9, 25)
    _grupo_quatro(dia)
    g = montar_gantt(dia)
    tronco = [p for p in g['produtos'] if p['tipo'] == 'base'][0]
    tarefas = tronco['tarefas']
    amassar = next(t for t in tarefas if t['etapa'] == 'Amassar base')
    pos = [t for t in tarefas
           if t['etapa'].startswith('Tirar') or t['etapa'].startswith('+')]
    assert pos, 'esperava retiradas/acréscimos no tronco'
    # a 1ª etapa pós-amassamento começa exatamente quando o amassamento termina
    assert min(t['ini'] for t in pos) == amassar['fim']
    # e as retiradas usam a amassadeira (não o padeiro)
    tirar = [t for t in tarefas if t['etapa'].startswith('Tirar')]
    assert tirar and all(t['recurso'] == 'amassadeira' for t in tirar)

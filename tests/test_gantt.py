"""Gantt da produção do dia: agenda etapas respeitando 1 amassadeira / 1 forno /
1 padeiro, com mise en place em paralelo e fermentação longa virando marcador."""
from datetime import date

from app.extensions import db
from app.models import (
    PlanejamentoItem,
    PlanejamentoProducao,
    Receita,
    ReceitaEtapa,
)
from app.services.gantt import PASSIVA_LONGA_MIN, montar_gantt


def _receita(nome, etapas, cap=50000, peso=1000.0):
    """etapas: lista de (nome, dur, equip, ativa)."""
    r = Receita(nome=nome, categoria='Pães', rendimento_qtd=10,
                rendimento_unidade='un', peso_base=peso,
                capacidade_amassadeira_g=cap)
    db.session.add(r)
    db.session.flush()
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
    assert mep_b['recurso'] == 'padeiro'
    assert am_a['recurso'] == 'amassadeira'


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

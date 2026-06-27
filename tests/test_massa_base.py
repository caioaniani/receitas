"""Massa-base com retiradas em cascata.

Verifica o cálculo com o EXEMPLO do dono (pão francês / sourdough trad /
sourdough 7 grãos saindo de uma base comum).
"""
from app.extensions import db
from app.models import (
    MassaBase,
    MassaBaseItem,
    Receita,
    ReceitaIngrediente,
)
from app.services.massa_base import calcular_cascata, ingredientes_por_porcao


def _receita(nome, ings, cap=50000):
    """ings: lista de (ingrediente, porcentagem). peso_base=1000 -> % vira g."""
    r = Receita(nome=nome, categoria='Pães', rendimento_qtd=10,
                rendimento_unidade='un', peso_base=1000.0,
                capacidade_amassadeira_g=cap)
    db.session.add(r)
    db.session.flush()
    for nome_ing, pct in ings:
        db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                          ingrediente_nome=nome_ing, porcentagem=pct))
    db.session.commit()
    return r


def _grupo(nome, receitas):
    mb = MassaBase(nome=nome)
    db.session.add(mb)
    db.session.flush()
    for i, r in enumerate(receitas):
        db.session.add(MassaBaseItem(massa_base_id=mb.id, receita_id=r.id, ordem=i))
    db.session.commit()
    return mb


def _exemplo_dono(app):
    pf = _receita('Pão Francês', [('Farinha', 100), ('Água', 70),
                                  ('Sal', 2), ('Levain', 20)])
    st = _receita('Sourdough Tradicional', [('Farinha', 100), ('Água', 80),
                                            ('Sal', 2), ('Levain', 20)])
    s7 = _receita('Sourdough 7 grãos', [('Farinha', 100), ('Água', 80),
                                        ('Sal', 2), ('Levain', 20), ('7 grãos', 15)])
    return pf, st, s7, _grupo('Base Sourdough', [pf, st, s7])


def test_ingredientes_por_porcao(app):
    pf, _, _, _ = _exemplo_dono(app)
    ing = ingredientes_por_porcao(pf)
    assert ing == {'Farinha': 1000.0, 'Água': 700.0, 'Sal': 20.0, 'Levain': 200.0}


def test_base_e_o_minimo_comum(app):
    _, _, _, mb = _exemplo_dono(app)
    c = calcular_cascata(mb)            # 1 porção de cada
    # base = mínimo comum: água 70% (a menor), grãos NÃO entra (só 1 receita tem)
    assert c['base'] == {'Farinha': 1000.0, 'Água': 700.0, 'Sal': 20.0, 'Levain': 200.0}
    assert '7 grãos' not in c['base']


def test_base_mix_e_massa_total(app):
    _, _, _, mb = _exemplo_dono(app)
    c = calcular_cascata(mb)            # 3 porções no total
    assert c['base_mix'] == {'Farinha': 3000.0, 'Água': 2100.0,
                             'Sal': 60.0, 'Levain': 600.0}
    assert c['base_massa'] == 5760.0   # 3000+2100+60+600 (o pico na amassadeira)
    assert c['fornadas'] == 1          # cabe em 50kg


def test_cascata_passos_batem_com_o_exemplo(app):
    pf, st, s7, mb = _exemplo_dono(app)
    c = calcular_cascata(mb)
    passos = {p['nome']: p for p in c['passos']}
    # 1) Pão Francês: nada a acrescentar, tira 1920 g
    assert passos['Pão Francês']['acrescentar'] == {}
    assert passos['Pão Francês']['tirar_massa'] == 1920.0
    # 2) Sourdough Tradicional: +água 200 g (100 g × 2 restantes), tira 2020 g
    assert passos['Sourdough Tradicional']['acrescentar'] == {'Água': 200.0}
    assert passos['Sourdough Tradicional']['tirar_massa'] == 2020.0
    # 3) Sourdough 7 grãos: +grãos 150 g (1 restante), tira 2170 g
    assert passos['Sourdough 7 grãos']['acrescentar'] == {'7 grãos': 150.0}
    assert passos['Sourdough 7 grãos']['tirar_massa'] == 2170.0
    assert c['avisos'] == []           # ordem é uma cadeia válida


def test_multiplicadores_escalam(app):
    pf, st, s7, mb = _exemplo_dono(app)
    # 2 porções de PF, 1 de ST, 1 de S7 -> 4 porções
    c = calcular_cascata(mb, {pf.id: 2, st.id: 1, s7.id: 1})
    assert c['total_porcoes'] == 4
    assert c['base_mix']['Farinha'] == 4000.0      # 1000 × 4
    assert c['base_mix']['Água'] == 2800.0         # 700 × 4
    passos = {p['nome']: p for p in c['passos']}
    # PF: 2 porções -> tira 1920 × 2 = 3840
    assert passos['Pão Francês']['tirar_massa'] == 3840.0
    # depois de tirar PF (2), sobram 2 (ST+S7): +água 100 × 2 = 200
    assert passos['Sourdough Tradicional']['acrescentar'] == {'Água': 200.0}


def test_fornadas_varias_batidas(app):
    pf, st, s7, mb = _exemplo_dono(app)
    # base 5760/porção-conjunto... força capacidade pequena: cap 3000 -> base
    # 5760 (3 porções) precisa de 2 batidas
    for r in (pf, st, s7):
        r.capacidade_amassadeira_g = 3000
    db.session.commit()
    c = calcular_cascata(mb)
    assert c['capacidade'] == 3000
    assert c['fornadas'] == 2          # ceil(5760/3000)


def test_ordem_invalida_gera_aviso(app):
    # ordem errada: 7 grãos ANTES do pão francês -> água precisaria diminuir
    pf = _receita('PF', [('Farinha', 100), ('Água', 70)])
    s7 = _receita('S7', [('Farinha', 100), ('Água', 80), ('Grãos', 15)])
    mb = _grupo('Base', [s7, pf])      # ordem ruim: s7 primeiro
    c = calcular_cascata(mb)
    assert any('DIMINUIR' in a for a in c['avisos'])


def test_grupo_vazio_retorna_none(app):
    mb = MassaBase(nome='Vazia')
    db.session.add(mb)
    db.session.commit()
    assert calcular_cascata(mb) is None

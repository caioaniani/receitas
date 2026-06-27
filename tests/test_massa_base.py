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


def _retiradas(c):
    return [p for p in c['passos'] if p['tipo'] == 'retirada']


def _incrementos(c):
    return [p for p in c['passos'] if p['tipo'] == 'incremento']


def test_cascata_linear_bate_com_o_exemplo(app):
    """3 pães em ordem de hidratação crescente; só o 7 grãos tem recheio (laranja).
    A água é um passo do TRONCO entre as retiradas."""
    pf, st, s7, mb = _exemplo_dono(app)
    c = calcular_cascata(mb)
    passos = c['passos']
    # ordem: tira PF (75%) -> +água -> tira Tradicional (80%) -> tira 7 grãos
    assert [p['tipo'] for p in passos] == [
        'retirada', 'incremento', 'retirada', 'retirada']
    # 1) Pão Francês: linha principal (verde), nada a bater, tira 1920 g
    assert passos[0]['nome'] == 'Pão Francês'
    assert passos[0]['eh_ramo'] is False
    assert passos[0]['acrescentar'] == {}
    assert passos[0]['tirar_massa'] == 1920.0
    # 2) incremento de água no tronco: 100 g × 2 porções restantes = 200 g
    assert passos[1]['acrescentar'] == {'Água': 200.0}
    # 3) Tradicional: linha principal (verde), tira 2020 g
    assert passos[2]['nome'] == 'Sourdough Tradicional'
    assert passos[2]['eh_ramo'] is False
    assert passos[2]['tirar_massa'] == 2020.0
    # 4) 7 grãos: recheio próprio (laranja), só os grãos batidos na porção
    assert passos[3]['nome'] == 'Sourdough 7 grãos'
    assert passos[3]['eh_ramo'] is True
    assert passos[3]['acrescentar'] == {'7 grãos': 150.0}
    assert passos[3]['tirar_massa'] == 2170.0
    assert c['avisos'] == []


def test_recheios_exclusivos_ramificam(app):
    """7 grãos × nozes e azeitonas: recheios exclusivos -> cada um vira RAMO
    (tira massa branca e recebe o seu recheio à parte). Sem aviso de DIMINUIR."""
    pf = _receita('Pão Francês', [('Farinha', 100), ('Água', 70),
                                  ('Sal', 2), ('Levain', 20)])
    st = _receita('Sourdough Tradicional', [('Farinha', 100), ('Água', 80),
                                            ('Sal', 2), ('Levain', 20)])
    s7 = _receita('Sourdough 7 grãos', [('Farinha', 100), ('Água', 80),
                                        ('Sal', 2), ('Levain', 20), ('7 grãos', 15)])
    na = _receita('Sourdough Nozes', [('Farinha', 100), ('Água', 80),
                                      ('Sal', 2), ('Levain', 20), ('Nozes', 25)])
    mb = _grupo('Base', [pf, st, s7, na])
    c = calcular_cascata(mb)
    assert c['avisos'] == []                       # árvore não precisa de ordem
    # linha principal: pão francês e tradicional (sem recheio próprio)
    lin = {p['nome'] for p in c['lineares'] if p['nome']}
    assert 'Pão Francês' in lin and 'Sourdough Tradicional' in lin
    # ramos: 7 grãos e nozes, cada um com o seu recheio
    ramos = {p['nome']: p for p in c['ramos']}
    assert set(ramos) == {'Sourdough 7 grãos', 'Sourdough Nozes'}
    assert ramos['Sourdough 7 grãos']['acrescentar'] == {'7 grãos': 150.0}
    assert ramos['Sourdough Nozes']['acrescentar'] == {'Nozes': 250.0}
    # cada ramo puxa a massa branca (base + água = 2020 g/porção) e finaliza
    assert ramos['Sourdough 7 grãos']['tirar_branca'] == 2020.0
    assert ramos['Sourdough 7 grãos']['tirar_massa'] == 2170.0
    assert ramos['Sourdough Nozes']['tirar_massa'] == 2270.0


def test_multiplicadores_escalam(app):
    pf, st, s7, mb = _exemplo_dono(app)
    # 2 porções de PF, 1 de ST, 1 de S7 -> 4 porções
    c = calcular_cascata(mb, {pf.id: 2, st.id: 1, s7.id: 1})
    assert c['total_porcoes'] == 4
    assert c['base_mix']['Farinha'] == 4000.0      # 1000 × 4
    assert c['base_mix']['Água'] == 2800.0         # 700 × 4
    lin = {p['nome']: p for p in c['lineares']}
    # PF: 2 porções -> tira 1920 × 2 = 3840
    assert lin['Pão Francês']['tirar_massa'] == 3840.0
    # depois de tirar PF (2), sobram 2 (ST+S7): +água 100 × 2 = 200
    assert lin['Sourdough Tradicional']['acrescentar'] == {'Água': 200.0}


def test_fornadas_varias_batidas(app):
    pf, st, s7, mb = _exemplo_dono(app)
    # base 5760 (3 porções) com capacidade 3000 -> precisa de 2 batidas
    for r in (pf, st, s7):
        r.capacidade_amassadeira_g = 3000
    db.session.commit()
    c = calcular_cascata(mb)
    assert c['capacidade'] == 3000
    assert c['fornadas'] == 2          # ceil(5760/3000)


def test_ordem_nao_importa_mais(app):
    """A ordem dos itens não muda o resultado — a árvore se organiza sozinha."""
    pf = _receita('PF', [('Farinha', 100), ('Água', 70)])
    s7 = _receita('S7', [('Farinha', 100), ('Água', 80), ('Grãos', 15)])
    mb = _grupo('Base', [s7, pf])      # ordem "ruim": s7 primeiro
    c = calcular_cascata(mb)
    assert c['avisos'] == []           # sem DIMINUIR
    # PF (menos água) sai primeiro na linha, mesmo cadastrado depois
    nomes = [p['nome'] for p in c['lineares'] if p['nome']]
    assert nomes[0] == 'PF'


def test_porcoes_fracionarias_escalam(app):
    """multiplicadores aceita fracionário (unidades/rendimento) — a base segue o
    consumo real, sem arredondar a fornada pra cima."""
    pf, st, s7, mb = _exemplo_dono(app)
    # 0,5 porção de cada -> base é metade da de 1 porção
    c = calcular_cascata(mb, {pf.id: 0.5, st.id: 0.5, s7.id: 0.5})
    assert abs(c['total_porcoes'] - 1.5) < 1e-9
    assert c['base_mix']['Farinha'] == 1500.0      # 1000 × 1,5
    assert c['base_mix']['Água'] == 1050.0         # 700 × 1,5
    lin = {p['nome']: p for p in c['lineares']}
    assert lin['Pão Francês']['tirar_massa'] == 960.0   # 1920 × 0,5


def test_grupo_vazio_retorna_none(app):
    mb = MassaBase(nome='Vazia')
    db.session.add(mb)
    db.session.commit()
    assert calcular_cascata(mb) is None


# ── rotas ────────────────────────────────────────────────────────────────────

def _login(app, user):
    c = app.test_client()
    c.post('/auth/login', data={'login': user.login, 'senha': '123'},
           follow_redirects=True)
    return c


def test_rota_lista_e_criar(app, admin_user):
    c = _login(app, admin_user)
    resp = c.post('/receitas/massa-base', data={'nome': 'Base Sourdough'},
                  follow_redirects=True)
    assert resp.status_code == 200
    mb = MassaBase.query.filter_by(nome='Base Sourdough').first()
    assert mb is not None
    # a criação redireciona pro editor
    assert ('massa-base/%d' % mb.id) in resp.request.path


def test_rota_add_e_ordem_e_calculo(app, admin_user):
    pf, st, s7, mb = _exemplo_dono(app)
    MassaBaseItem.query.delete()      # começa vazio pra testar o add via rota
    db.session.commit()
    c = _login(app, admin_user)
    for r in (pf, st, s7):
        c.post('/receitas/massa-base/%d' % mb.id,
               data={'acao': 'add', 'receita_id': r.id}, follow_redirects=True)
    assert MassaBaseItem.query.filter_by(massa_base_id=mb.id).count() == 3
    # o editor mostra o cálculo (base 5,76 kg, retira pão francês)
    resp = c.get('/receitas/massa-base/%d' % mb.id)
    html = resp.get_data(as_text=True)
    assert 'Tirar Pão Francês' in html
    assert '5.76 kg' in html or '5,76 kg' in html


def test_rota_remover_item(app, admin_user):
    pf, st, s7, mb = _exemplo_dono(app)
    c = _login(app, admin_user)
    c.post('/receitas/massa-base/%d' % mb.id,
           data={'acao': 'remover', 'receita_id': st.id}, follow_redirects=True)
    restantes = {it.receita_id for it in
                 MassaBaseItem.query.filter_by(massa_base_id=mb.id).all()}
    assert restantes == {pf.id, s7.id}        # só o tradicional saiu


def test_rota_add_receita_ja_em_grupo_recusa(app, admin_user):
    pf, st, s7, mb = _exemplo_dono(app)   # pf já está no grupo
    outro = MassaBase(nome='Outro')
    db.session.add(outro)
    db.session.commit()
    c = _login(app, admin_user)
    c.post('/receitas/massa-base/%d' % outro.id,
           data={'acao': 'add', 'receita_id': pf.id}, follow_redirects=True)
    # pf não pode estar em dois grupos
    assert MassaBaseItem.query.filter_by(receita_id=pf.id).count() == 1


def test_rota_excluir_grupo(app, admin_user):
    _, _, _, mb = _exemplo_dono(app)
    mbid = mb.id
    c = _login(app, admin_user)
    c.post('/receitas/massa-base/%d' % mbid,
           data={'acao': 'excluir'}, follow_redirects=True)
    assert MassaBase.query.get(mbid) is None
    assert MassaBaseItem.query.filter_by(massa_base_id=mbid).count() == 0


def test_rota_exige_admin(app):
    from app.models import Usuario
    u = Usuario(nome='func', login='func', papel='funcionario')
    u.set_senha('123')
    db.session.add(u)
    db.session.commit()
    c = _login(app, u)
    assert c.get('/receitas/massa-base').status_code == 403

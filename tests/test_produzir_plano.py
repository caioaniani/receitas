"""Etapa 3 (opção B): produzir um item do plano credita estoque pronto e
desconta MP da ficha, proporcional às unidades.
"""
from app.extensions import db
from app.models import (
    EstoqueProducao,
    MateriaPrima,
    PlanejamentoItem,
    PlanejamentoProducao,
    Receita,
    ReceitaIngrediente,
)
from app.services.producao import produzir_item_plano
from app.utils import hoje


def _setup_item(rendimento=10, peso_base=1000.0, farinha_estoque=5000):
    mp = MateriaPrima(nome='Farinha', unidade='g', custo_por_kg=5.0,
                      estoque_atual=farinha_estoque)
    db.session.add(mp)
    db.session.commit()
    r = Receita(nome='Pão Teste', categoria='Paes', rendimento_qtd=rendimento,
                rendimento_unidade='un', peso_base=peso_base)
    db.session.add(r)
    db.session.flush()
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                      ingrediente_nome='Farinha', porcentagem=100))
    plano = PlanejamentoProducao(data=hoje(), origem='cronograma',
                                 status='aprovado', nome='Cronograma')
    db.session.add(plano)
    db.session.flush()
    it = PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                          multiplicador=2, qtd_alvo=20)
    db.session.add(it)
    db.session.commit()
    return r, mp, it


def test_produzir_credita_estoque_e_baixa_mp(app, admin_user):
    r, mp, it = _setup_item(rendimento=10, peso_base=1000.0,
                            farinha_estoque=5000)
    res = produzir_item_plano(it.id, 10, admin_user.id)
    assert res['ok'] is True
    assert res['produzido'] == 10

    ep = EstoqueProducao.query.filter_by(receita_id=r.id).first()
    assert ep is not None and ep.quantidade == 10       # 10 un creditadas

    db.session.refresh(mp)
    assert mp.estoque_atual == 4000   # 10un/10 = 1 base = 1000g farinha; 5000-1000

    db.session.refresh(it)
    assert it.produzido_qtd == 10


def test_produzir_acumula(app, admin_user):
    r, mp, it = _setup_item()
    produzir_item_plano(it.id, 10, admin_user.id)
    produzir_item_plano(it.id, 5, admin_user.id)
    db.session.refresh(it)
    assert it.produzido_qtd == 15


def test_produzir_qtd_invalida(app, admin_user):
    r, mp, it = _setup_item()
    res = produzir_item_plano(it.id, 0, admin_user.id)
    assert res['ok'] is False
    db.session.refresh(it)
    assert it.produzido_qtd == 0


def test_rota_produzir_plano(app, admin_user):
    r, mp, it = _setup_item()
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.post('/padeiro-testes/produzir-plano/%d' % it.id,
                       data={'unidades': 10})
    assert resp.status_code == 302
    db.session.refresh(it)
    assert it.produzido_qtd == 10


def test_padeiro_testes_mostra_producao_do_dia(app, admin_user):
    r, mp, it = _setup_item()
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/padeiro-testes/')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Produção do dia' in body
    assert 'Pão Teste' in body


def test_plano_do_dia_agrupa_por_massa_base(app, admin_user):
    """Pães de uma massa-base comum aparecem agrupados (amasse a base + tire
    cada um); os demais vão pra 'solos'."""
    from app.blueprints.padeiro_testes.routes import _plano_do_dia
    from app.models import MassaBase, MassaBaseItem

    def _rec(nome, agua, recheio=None):
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
        db.session.commit()
        return r

    pf = _rec('Pão Francês', 70)
    s7 = _rec('Sourdough 7g', 80, ('Grãos', 40))
    foc = _rec('Focaccia', 65)               # solo (sem massa-base)
    mb = MassaBase(nome='Sourdough')
    db.session.add(mb)
    db.session.flush()
    for r in (pf, s7):
        db.session.add(MassaBaseItem(massa_base_id=mb.id, receita_id=r.id))
    plano = PlanejamentoProducao(data=hoje(), origem='cronograma')
    db.session.add(plano)
    db.session.flush()
    for r in (pf, s7, foc):
        db.session.add(PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                                        multiplicador=1, qtd_alvo=10))
    db.session.commit()

    p = _plano_do_dia(hoje())
    assert len(p['grupos']) == 1
    g = p['grupos'][0]
    assert g['nome'] == 'Sourdough'
    assert g['mb_id'] == mb.id
    assert g['base_massa_label']                        # ex "3,4 kg"
    assert {i['nome'] for i in g['itens']} == {'Pão Francês', 'Sourdough 7g'}
    assert {i['nome'] for i in p['solos']} == {'Focaccia'}


def test_massa_base_mise_endpoint(app, admin_user):
    """A rota /massa-base/<id>.json devolve o que pôr na amassadeira (base
    escalada pro plano) + a sequência de retiradas em unidades de pão."""
    from app.models import MassaBase, MassaBaseItem

    def _rec(nome, agua, recheio=None):
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
        db.session.commit()
        return r

    pf = _rec('Pão Francês', 70)
    s7 = _rec('Sourdough 7g', 80, ('Grãos', 40))
    mb = MassaBase(nome='Sourdough')
    db.session.add(mb)
    db.session.flush()
    for r in (pf, s7):
        db.session.add(MassaBaseItem(massa_base_id=mb.id, receita_id=r.id))
    plano = PlanejamentoProducao(data=hoje(), origem='cronograma')
    db.session.add(plano)
    db.session.flush()
    db.session.add(PlanejamentoItem(planejamento_id=plano.id, receita_id=pf.id,
                                    multiplicador=2, qtd_alvo=20))
    db.session.add(PlanejamentoItem(planejamento_id=plano.id, receita_id=s7.id,
                                    multiplicador=1, qtd_alvo=10))
    db.session.commit()

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/padeiro-testes/massa-base/%d.json?data=%s'
                      % (mb.id, hoje().isoformat()))
    assert resp.status_code == 200
    d = resp.get_json()
    assert d['nome'] == 'Sourdough'
    assert not d.get('vazio')
    # base = mínimo comum (Farinha + Água a 70%); Grãos NÃO entra na base
    nomes_base = {b['nome'] for b in d['base_recipe']}
    assert 'Farinha' in nomes_base and 'Água' in nomes_base
    assert 'Grãos' not in nomes_base
    # sequência: tira Pão Francês (20), depois acrescenta e tira Sourdough 7g (10)
    passos = {p['nome']: p for p in d['cascata'] if p['nome']}
    assert passos['Pão Francês']['unidades'] == 20
    assert passos['Sourdough 7g']['unidades'] == 10
    acr = {a['nome'] for a in passos['Sourdough 7g']['acrescentar']}
    assert 'Grãos' in acr        # o recheio só entra na retirada do 7 grãos
    # mostra os GRAMAS de massa a tirar (não só as unidades): PF = 1700 g/porção
    # × 2 porções = 3,40 kg; 7g = 2200 g × 1 = 2,20 kg
    assert passos['Pão Francês']['tirar_massa'] == '3,40 kg'
    assert passos['Sourdough 7g']['tirar_massa'] == '2,20 kg'


def test_massa_base_mise_escala_unidades_reais(app, admin_user):
    """A base escala pelas UNIDADES reais (qtd_alvo / rendimento), não pelo
    multiplicador inteiro arredondado — senão 5 un de rendimento 10 inflaria a
    massa pro dobro (1 fornada cheia)."""
    from app.models import MassaBase, MassaBaseItem

    r = Receita(nome='Pão X', categoria='Pães', rendimento_qtd=10,
                rendimento_unidade='un', peso_base=1000.0,
                capacidade_amassadeira_g=50000)
    db.session.add(r); db.session.flush()
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                      ingrediente_nome='Farinha', porcentagem=100))
    db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                      ingrediente_nome='Água', porcentagem=70))
    mb = MassaBase(nome='Base X'); db.session.add(mb); db.session.flush()
    db.session.add(MassaBaseItem(massa_base_id=mb.id, receita_id=r.id))
    plano = PlanejamentoProducao(data=hoje(), origem='cronograma')
    db.session.add(plano); db.session.flush()
    # qtd_alvo 5 de rendimento 10 -> 0,5 porção; multiplicador legado = ceil = 1
    db.session.add(PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                                    multiplicador=1, qtd_alvo=5))
    db.session.commit()

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    d = client.get('/padeiro-testes/massa-base/%d.json?data=%s'
                   % (mb.id, hoje().isoformat())).get_json()
    # massa/porção = 1000 + 700 = 1700 g; 0,5 porção -> 850 g (NÃO 1700)
    base = {b['nome']: b['qtd'] for b in d['base_recipe']}
    assert base['Farinha'] == '500 g'        # 1000 × 0,5
    assert base['Água'] == '350 g'           # 700 × 0,5
    passo = d['cascata'][0]
    assert passo['unidades'] == 5
    assert passo['tirar_massa'] == '850 g'    # 1700 × 0,5, não 1700


def test_massa_base_mise_exige_padeiro(app):
    """Sem papel padeiro/admin a rota recusa (decorator @padeiro_required)."""
    from app.models import MassaBase, Usuario

    mb = MassaBase(nome='Qualquer')
    db.session.add(mb)
    u = Usuario(nome='func', login='func2', papel='funcionario')
    u.set_senha('123')
    db.session.add(u)
    db.session.commit()
    client = app.test_client()
    client.post('/auth/login', data={'login': 'func2', 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/padeiro-testes/massa-base/%d.json' % mb.id)
    assert resp.status_code == 403

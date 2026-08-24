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
    resp = client.post('/padeiro/produzir-plano/%d' % it.id,
                       data={'unidades': 10})
    assert resp.status_code == 302
    db.session.refresh(it)
    assert it.produzido_qtd == 10


def test_padeiro_testes_mostra_producao_do_dia(app, admin_user):
    r, mp, it = _setup_item()
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    resp = client.get('/padeiro/')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Produção do dia' in body
    assert 'Pão Teste' in body
    assert '1 item para concluir' in body
    assert 'Registrar produção' in body
    assert 'css/padeiro-v2.css' in body
    assert 'O que produzir agora' in body
    assert 'Ferramentas rápidas' in body
    for controle in ('hist-toggle', 'cong-toggle', 'prep-toggle',
                      'prod-toggle', 'fichas-toggle'):
        assert body.count(f'id="{controle}"') == 1


def test_plano_do_dia_agrupa_por_massa_base(app, admin_user):
    """Pães de uma massa-base comum aparecem agrupados (amasse a base + tire
    cada um); os demais vão pra 'solos'."""
    from app.blueprints.padeiro.routes import _plano_do_dia
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
    assert p['total_itens'] == 3
    assert p['itens_pendentes'] == 3
    assert p['itens_concluidos'] == 0
    assert p['progresso_pct'] == 0


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
    resp = client.get('/padeiro/massa-base/%d.json?data=%s'
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
    # × 2 porções = 3,4 kg. O 7g tira só a massa branca (1800 g): os 400 g de
    # grãos entram DEPOIS, batidos na porção — não contam no "tire X kg".
    assert passos['Pão Francês']['tirar_massa'] == '3,4 kg'
    assert passos['Sourdough 7g']['tirar_massa'] == '1,8 kg'


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
    d = client.get('/padeiro/massa-base/%d.json?data=%s'
                   % (mb.id, hoje().isoformat())).get_json()
    # massa/porção = 1000 + 700 = 1700 g; 0,5 porção -> 850 g (NÃO 1700)
    base = {b['nome']: b['qtd'] for b in d['base_recipe']}
    assert base['Farinha'] == '500 g'        # 1000 × 0,5
    assert base['Água'] == '350 g'           # 700 × 0,5
    passo = d['cascata'][0]
    assert passo['unidades'] == 5
    assert passo['tirar_massa'] == '850 g'    # 1700 × 0,5, não 1700


def test_massa_base_mise_usa_massa_crua_sem_perda(app, admin_user):
    """120 un de 500 g de massa CRUA = 60 kg de massa, independente da perda do
    forno e do rendimento_qtd digitado (a produção pesa massa crua). Decisão do
    dono 30/06: a perda jamais entra nessa conta."""
    from app.models import MassaBase, MassaBaseItem

    r = Receita(nome='Sourdough 7g', categoria='Pães', rendimento_qtd=3,
                rendimento_unidade='un', peso_base=1000.0, peso_unitario=500.0,
                perda_percentual=30, capacidade_amassadeira_g=200000)
    db.session.add(r)
    db.session.flush()
    for nome, pct in [('Farinha', 100), ('Água', 85), ('Levain', 20),
                      ('Sal', 2), ('Fermento', 0.1), ('Grãos', 10)]:
        db.session.add(ReceitaIngrediente(receita_id=r.id, tipo='mp',
                                          ingrediente_nome=nome, porcentagem=pct))
    mb = MassaBase(nome='Base')
    db.session.add(mb)
    db.session.flush()
    db.session.add(MassaBaseItem(massa_base_id=mb.id, receita_id=r.id))
    plano = PlanejamentoProducao(data=hoje(), origem='cronograma')
    db.session.add(plano)
    db.session.flush()
    db.session.add(PlanejamentoItem(planejamento_id=plano.id, receita_id=r.id,
                                    multiplicador=1, qtd_alvo=120))
    db.session.commit()

    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)
    d = client.get('/padeiro/massa-base/%d.json?data=%s'
                   % (mb.id, hoje().isoformat())).get_json()
    # 1 receita só: a base é a massa inteira. 120 × 500 g = 60 kg (rendimento_qtd=3
    # e perda=30 NÃO entram; massa/peso_unitario = 4,342 dá 60 kg, não 86,8 kg).
    assert d['base_massa'] == '60 kg'
    ret = next(p for p in d['cascata'] if p['tipo'] == 'retirada')
    assert ret['unidades'] == 120


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
    resp = client.get('/padeiro/massa-base/%d.json' % mb.id)
    assert resp.status_code == 403


# ── consumo de sub-receita pronta (croissant almond consome tradicional) ──────

def _almond_setup(trad_nome, almond_nome, estoque_trad, link_fk=True):
    from app.services.estoque_congelados import entrada_producao
    trad = Receita(nome=trad_nome, categoria='Viennoiserie', rendimento_qtd=10,
                   rendimento_unidade='un', peso_base=1000.0)
    db.session.add(trad)
    db.session.flush()
    almond = Receita(nome=almond_nome, categoria='Viennoiserie', rendimento_qtd=10,
                     rendimento_unidade='un', peso_base=1000.0)
    db.session.add(almond)
    db.session.flush()
    # almond usa 10 tradicional por batida (rendimento 10) -> 1 por unidade
    db.session.add(ReceitaIngrediente(
        receita_id=almond.id, tipo='receita', ingrediente_nome=trad_nome,
        porcentagem=10, sub_receita_id=(trad.id if link_fk else None)))
    if estoque_trad:
        entrada_producao(receita_id=trad.id, quantidade=estoque_trad, usuario_id=1)
    plano = PlanejamentoProducao(data=hoje(), origem='cronograma')
    db.session.add(plano)
    db.session.flush()
    it = PlanejamentoItem(planejamento_id=plano.id, receita_id=almond.id,
                          multiplicador=1, qtd_alvo=10)
    db.session.add(it)
    db.session.commit()
    return trad, almond, it


def test_produzir_consome_subreceita_congelada(app, admin_user):
    trad, almond, it = _almond_setup('Croissant Tradicional', 'Croissant Almond', 8)
    res = produzir_item_plano(it.id, 5, admin_user.id)
    assert res['ok'] is True
    # baixou 5 tradicionais (8 -> 3)
    assert EstoqueProducao.query.filter_by(receita_id=trad.id).first().quantidade == 3
    # e o almond entrou no congelado
    assert EstoqueProducao.query.filter_by(receita_id=almond.id).first().quantidade == 5


def test_produzir_subreceita_resolve_por_nome_sem_fk(app, admin_user):
    """Mesmo sem a FK setada, resolve a sub-receita pelo nome (fallback)."""
    trad, almond, it = _almond_setup('Cro Trad NF', 'Almond NF', 6, link_fk=False)
    produzir_item_plano(it.id, 4, admin_user.id)
    assert EstoqueProducao.query.filter_by(receita_id=trad.id).first().quantidade == 2


def test_produzir_subreceita_sem_estoque_nao_negativa(app, admin_user):
    from app.models import MovEstoqueProducao
    trad, almond, it = _almond_setup('Cro Trad SE', 'Almond SE', 2)
    produzir_item_plano(it.id, 5, admin_user.id)   # quer 5, só tem 2
    assert EstoqueProducao.query.filter_by(receita_id=trad.id).first().quantidade == 0
    deficit = MovEstoqueProducao.query.filter_by(
        tipo='consumo_subreceita_sem_estoque').first()
    assert deficit is not None and deficit.quantidade == 3


def test_produzir_freeform_consome_subreceita(app, admin_user):
    """O painel 'Produzir' da TV (rota /produzir) também consome a sub-receita
    pronta do congelado ao produzir uma receita derivada."""
    trad, almond, _ = _almond_setup('Cro Trad TV', 'Almond TV', 7)
    c = app.test_client()
    c.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
           follow_redirects=True)
    resp = c.post('/padeiro/produzir',
                  json={'itens': [{'ref': 'receita:%d' % almond.id, 'quantidade': 3}]})
    assert resp.status_code == 200 and resp.get_json()['ok'] is True
    assert EstoqueProducao.query.filter_by(receita_id=trad.id).first().quantidade == 4


# ── editar a ordem de produção do dia (cronograma) ────────────────────────────

def test_editar_plano_muda_qtd_adiciona_remove(app, admin_user):
    r1 = Receita(nome='Pão Edit A', categoria='Pães', rendimento_qtd=10,
                 rendimento_unidade='un', peso_base=1000.0)
    r2 = Receita(nome='Pão Edit B', categoria='Pães', rendimento_qtd=10,
                 rendimento_unidade='un', peso_base=1000.0)
    r3 = Receita(nome='Pão Edit C', categoria='Pães', rendimento_qtd=10,
                 rendimento_unidade='un', peso_base=1000.0)
    db.session.add_all([r1, r2, r3]); db.session.flush()
    plano = PlanejamentoProducao(data=hoje(), origem='cronograma')
    db.session.add(plano); db.session.flush()
    it1 = PlanejamentoItem(planejamento_id=plano.id, receita_id=r1.id,
                           multiplicador=2, qtd_alvo=20)
    it2 = PlanejamentoItem(planejamento_id=plano.id, receita_id=r2.id,
                           multiplicador=1, qtd_alvo=10)
    db.session.add_all([it1, it2]); db.session.commit()
    it1_id, it2_id, r1id, r2id, r3id = it1.id, it2.id, r1.id, r2.id, r3.id

    c = app.test_client()
    c.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
           follow_redirects=True)
    resp = c.post('/padeiro/plano/editar', data={
        'data': hoje().isoformat(),
        'alvo_%d' % it1_id: '50',            # A: 20 -> 50
        'remover_%d' % it2_id: 'on',         # remove B
        'novo_receita_id[]': str(r3id),
        'novo_alvo[]': '30',                 # adiciona C: 30
    })
    assert resp.status_code in (302, 303)
    db.session.expire_all()
    plano = PlanejamentoProducao.query.filter_by(
        data=hoje(), origem='cronograma').first()
    por = {i.receita_id: i for i in plano.itens}
    assert por[r1id].qtd_alvo == 50 and por[r1id].multiplicador == 5  # ceil(50/10)
    assert r2id not in por                                            # removido
    assert por[r3id].qtd_alvo == 30


def test_editar_plano_exige_admin(app):
    from app.models import Usuario
    u = Usuario(nome='pad', login='pad9', papel='padeiro')
    u.set_senha('123')
    db.session.add(u); db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'pad9', 'senha': '123'},
           follow_redirects=True)
    assert c.get('/padeiro/plano/editar').status_code == 403

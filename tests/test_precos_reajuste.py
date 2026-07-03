"""Reajuste de preços em massa em REAIS (02/07/2026, decisão do dono):
avulso +valor; cesta/kit de verdade +valor fixo + valor × unidades (unitário
conta pela quantidade; porção em g/ml conta 1); composto de item único
(croissant recheado, porção de frios) sobe como AVULSO; item sem o preço
cadastrado fica intocado. Fluxo prévia → aplicar (owner)."""
from app.extensions import db
from app.models import MateriaPrima, Produto, ProdutoItem, Receita
from app.services.precos_reajuste import aplicar_reajuste, previa_reajuste


def _receita(nome, **kw):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0, **kw)
    db.session.add(r)
    db.session.commit()
    return r


def _cesta(nome, itens, mps=(), **kw):
    """itens = [(receita, qtd)]; mps = [(materia_prima, qtd)]."""
    p = Produto(nome=nome, categoria='Cestas', ativo=True, **kw)
    db.session.add(p)
    db.session.flush()
    for rec, qtd in itens:
        db.session.add(ProdutoItem(produto_id=p.id, tipo='receita',
                                   receita_id=rec.id, item_nome=rec.nome,
                                   quantidade=qtd))
    for mp, qtd in mps:
        db.session.add(ProdutoItem(produto_id=p.id, tipo='mp',
                                   materia_prima_id=mp.id, item_nome=mp.nome,
                                   quantidade=qtd))
    db.session.commit()
    return p


def _mp(nome, unidade='g'):
    mp = MateriaPrima(nome=nome, unidade=unidade, custo_por_kg=10.0)
    db.session.add(mp)
    db.session.commit()
    return mp


def test_previa_avulso_cesta_e_sem_preco(app):
    r1 = _receita('Sourdough', preco_site=30.0)
    r2 = _receita('Baguete')                     # sem preco_site: intocada
    cesta = _cesta('Cesta Café', [(r1, 2), (r2, 1)], preco_site=100.0)
    simples = Produto(nome='Granola', categoria='Outros', ativo=True,
                      preco_site=25.0)
    db.session.add(simples)
    db.session.commit()

    previa = previa_reajuste('preco_site', 2.0)
    por_nome = {ln['nome']: ln for ln in previa['linhas']}

    assert por_nome['Sourdough']['aumento'] == 2.0
    assert por_nome['Sourdough']['preco_novo'] == 32.0
    assert 'Baguete' not in por_nome            # sem preço → fora da prévia
    assert previa['pulados_sem_preco'] >= 1
    # cesta com 3 unidades vendáveis: 2,00 fixo + 2,00×3 = 8,00
    assert por_nome['Cesta Café']['tipo'] == 'cesta'
    assert por_nome['Cesta Café']['unidades'] == 3
    assert por_nome['Cesta Café']['aumento'] == 8.0
    assert por_nome['Cesta Café']['preco_novo'] == 108.0
    assert por_nome['Granola']['tipo'] == 'produto'
    assert por_nome['Granola']['aumento'] == 2.0
    _ = cesta


def test_porcao_em_gramas_conta_um_nao_a_gramagem(app):
    """Bug pego pelo dono na 1ª prévia: 100g de nutella contava 100 unidades
    (croissant de nutella saía +R$ 204). Porção em g/ml conta 1; e composto
    com <= 1 unidade vendável sobe como AVULSO."""
    r_croissant = _receita('Croissant Retorno', preco_site=10.0)
    nutella = _mp('Nutella', unidade='g')
    croissant_nutella = _cesta('Croissant de nutella', [(r_croissant, 1)],
                               mps=[(nutella, 100)], preco_site=30.5)
    # Bandeja de verdade: 3 pães + 2 porções de frios em gramas
    mussarela = _mp('Mussarela', unidade='g')
    bandeja = _cesta('Bandeja', [(r_croissant, 3)],
                     mps=[(mussarela, 100), (nutella, 50)], preco_site=100.0)

    previa = previa_reajuste('preco_site', 2.0)
    por_nome = {ln['nome']: ln for ln in previa['linhas']}
    # croissant recheado: 1 vendável + recheio → AVULSO +2 (não +204, não +4)
    assert por_nome['Croissant de nutella']['tipo'] == 'composto'
    assert por_nome['Croissant de nutella']['aumento'] == 2.0
    # bandeja: 3 vendáveis + 2 porções = 5 → 2 + 2×5 = 12
    assert por_nome['Bandeja']['tipo'] == 'cesta'
    assert por_nome['Bandeja']['unidades'] == 5
    assert por_nome['Bandeja']['aumento'] == 12.0
    _ = croissant_nutella, bandeja


def test_mp_em_unidades_conta_pela_quantidade(app):
    """MP vendida por unidade (ex: pão de queijo congelado 'un') conta pela
    quantidade, não como porção."""
    r_pao = _receita('Pão Sourdough', preco_site=25.0)
    pdq = _mp('Pão de Queijo Congelado', unidade='un')
    cesta = _cesta('Kit Festa', [(r_pao, 2)], mps=[(pdq, 10)],
                   preco_site=80.0)
    previa = previa_reajuste('preco_site', 2.0)
    ln = next(x for x in previa['linhas'] if x['nome'] == 'Kit Festa')
    assert ln['unidades'] == 12                  # 2 pães + 10 pães de queijo
    assert ln['aumento'] == 26.0                 # 2 + 2×12
    _ = cesta


def test_aplicar_persiste_e_nao_toca_sem_preco(app):
    r1 = _receita('Sourdough', preco_site=30.0, preco_loja=25.0)
    r2 = _receita('Baguete')
    cesta = _cesta('Cesta Café', [(r1, 2)], preco_site=100.0)

    n = aplicar_reajuste('preco_site', 2.0)
    db.session.commit()
    assert n == 2                                # r1 + cesta
    db.session.refresh(r1)
    db.session.refresh(r2)
    db.session.refresh(cesta)
    assert r1.preco_site == 32.0
    assert r1.preco_loja == 25.0                 # outro campo intocado
    assert r2.preco_site is None                 # sem preço segue sem preço
    assert cesta.preco_site == 106.0             # 100 + (2 + 2×2)


def test_atacado_mapeia_preco_venda_da_receita(app):
    r = _receita('Croissant', preco_venda=20.0)
    aplicar_reajuste('preco_atacado', 2.0)
    db.session.commit()
    db.session.refresh(r)
    assert r.preco_venda == 22.0


def test_fluxo_rotas_previa_e_aplicar(app, owner_user):
    r = _receita('Sourdough', preco_site=30.0)
    client = app.test_client()
    client.post('/auth/login', data={'login': owner_user.login, 'senha': '123'})

    resp = client.post('/receitas/precos/reajuste/previa',
                       data={'campo': 'preco_site', 'valor': '2,00'})
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'nada foi alterado ainda' in html
    assert 'Sourdough' in html
    assert f'aum|receita|{r.id}' in html         # coluna Aumento editável
    db.session.refresh(r)
    assert r.preco_site == 30.0                  # prévia não grava

    resp = client.post('/receitas/precos/reajuste/aplicar', data={
        'campo': 'preco_site', 'valor': '2,00',
        f'aum|receita|{r.id}': '2,00'})
    assert resp.status_code == 302
    db.session.refresh(r)
    assert r.preco_site == 32.0


def test_aplicar_usa_aumento_editado_na_previa(app, owner_user):
    """Caso Granola 500g: a fórmula sugeriu +12 (5×100g é composição técnica,
    não cesta) e o dono corrige para +2 na prévia — vale o valor editado.
    Linha ZERADA não é alterada."""
    granola = _cesta('Granola Artesanal 500g',
                     [(_receita('Granola 100g', preco_site=19.0), 5)],
                     preco_site=49.0)
    outra = _receita('Sourdough', preco_site=30.0)
    client = app.test_client()
    client.post('/auth/login', data={'login': owner_user.login, 'senha': '123'})

    resp = client.post('/receitas/precos/reajuste/aplicar', data={
        'campo': 'preco_site', 'valor': '2,00',
        f'aum|produto|{granola.id}': '2,00',     # editado: 12 -> 2
        f'aum|receita|{outra.id}': '0',          # zerado: não mexe
    })
    assert resp.status_code == 302
    db.session.refresh(granola)
    db.session.refresh(outra)
    assert granola.preco_site == 51.0            # 49 + 2 (editado)
    assert outra.preco_site == 30.0              # zerado ficou intocado


def test_reajuste_exige_owner(app, admin_user):
    """Admin comum não aplica reajuste em massa — e nenhum preço muda."""
    r = _receita('Sourdough', preco_site=30.0)
    client = app.test_client()
    client.post('/auth/login', data={'login': admin_user.login, 'senha': '123'})
    resp = client.post('/receitas/precos/reajuste/aplicar',
                       data={'campo': 'preco_site', 'valor': '2,00'})
    assert resp.status_code in (302, 403)
    db.session.refresh(r)
    assert r.preco_site == 30.0

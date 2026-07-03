"""Reajuste de preços em massa em REAIS (02/07/2026, decisão do dono):
avulso +valor; cesta/kit +valor fixo + valor × unidades dentro; item sem o
preço cadastrado fica intocado. Fluxo prévia → aplicar (owner)."""
from app.extensions import db
from app.models import Produto, ProdutoItem, Receita
from app.services.precos_reajuste import aplicar_reajuste, previa_reajuste


def _receita(nome, **kw):
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0, **kw)
    db.session.add(r)
    db.session.commit()
    return r


def _cesta(nome, itens, **kw):
    """itens = [(receita, qtd)]"""
    p = Produto(nome=nome, categoria='Cestas', ativo=True, **kw)
    db.session.add(p)
    db.session.flush()
    for rec, qtd in itens:
        db.session.add(ProdutoItem(produto_id=p.id, tipo='receita',
                                   receita_id=rec.id, item_nome=rec.nome,
                                   quantidade=qtd))
    db.session.commit()
    return p


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
    # cesta com 3 unidades: 2,00 fixo + 2,00×3 = 8,00
    assert por_nome['Cesta Café']['tipo'] == 'cesta'
    assert por_nome['Cesta Café']['unidades'] == 3
    assert por_nome['Cesta Café']['aumento'] == 8.0
    assert por_nome['Cesta Café']['preco_novo'] == 108.0
    assert por_nome['Granola']['tipo'] == 'produto'
    assert por_nome['Granola']['aumento'] == 2.0
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
    db.session.refresh(r)
    assert r.preco_site == 30.0                  # prévia não grava

    resp = client.post('/receitas/precos/reajuste/aplicar',
                       data={'campo': 'preco_site', 'valor': '2,00'})
    assert resp.status_code == 302
    db.session.refresh(r)
    assert r.preco_site == 32.0


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

"""Regressao: tela `/receitas/precos` aceita edicao em LOTE de
Receita + Produto simples + Cesta (todos no mesmo POST).

Antes (24/06/2026) so cobria Receita. O dono pediu pra editar tambem
cestas/produtos sem entrar item por item.
"""


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _csrf(client):
    """Pega o token CSRF da pagina GET pra reusar no POST."""
    resp = client.get('/receitas/precos')
    assert resp.status_code == 200
    html = resp.data.decode()
    marca = 'name="csrf_token" value="'
    i = html.index(marca) + len(marca)
    return html[i:html.index('"', i)]


def test_precos_lote_atualiza_receita_e_produto_e_cesta(app, admin_user):
    from app.extensions import db
    from app.models import Produto, ProdutoItem, Receita

    with app.app_context():
        receita = Receita(nome='Pao Frances Lote', categoria='Paes',
                          rendimento_qtd=1, rendimento_unidade='un',
                          peso_base=50.0, preco_loja=1.0)
        prod = Produto(nome='Cafe 250g Lote', categoria='Cafes',
                       preco_loja=20.0, ativo=True)
        cesta = Produto(nome='Box Mimo Lote', categoria='Cestas',
                        preco_loja=100.0, ativo=True)
        db.session.add_all([receita, prod, cesta])
        db.session.commit()
        # Cesta = Produto com itens. Adiciona 1 componente pra entrar na lista
        # de cestas (e nao na de produtos simples).
        db.session.add(ProdutoItem(produto_id=cesta.id, tipo='receita',
                                    receita_id=receita.id,
                                    item_nome='componente', quantidade=1))
        db.session.commit()
        rid, pid, cid = receita.id, prod.id, cesta.id

    client = app.test_client()
    _login(client, admin_user)
    token = _csrf(client)

    resp = client.post('/receitas/precos', data={
        'csrf_token': token,
        # Receita (sem prefixo p)
        f'preco_loja_{rid}': '2,50',
        f'preco_site_{rid}': '3,00',
        f'preco_venda_{rid}': '1,80',
        # Produto simples (prefixo p)
        f'preco_loja_p{pid}': '22,00',
        f'preco_site_p{pid}': '25,00',
        f'preco_atacado_p{pid}': '18,00',
        # Cesta (mesmo prefixo p — eh Produto tambem)
        f'preco_loja_p{cid}': '120,00',
        f'preco_site_p{cid}': '130,00',
        f'preco_atacado_p{cid}': '95,00',
    }, follow_redirects=False)
    assert resp.status_code in (302, 303)

    with app.app_context():
        r = Receita.query.get(rid)
        assert r.preco_loja == 2.50
        assert r.preco_site == 3.00
        assert r.preco_venda == 1.80

        p = Produto.query.get(pid)
        assert p.preco_loja == 22.00
        assert p.preco_site == 25.00
        assert p.preco_atacado == 18.00

        c = Produto.query.get(cid)
        assert c.preco_loja == 120.00
        assert c.preco_site == 130.00
        assert c.preco_atacado == 95.00


def test_precos_lote_nao_zera_item_ausente_no_form(app, admin_user):
    """Item nao enviado no POST mantem preco — protecao contra zerar
    arquivados/itens fora do scroll. Replica o invariante da Receita
    pro Produto."""
    from app.extensions import db
    from app.models import Produto, Receita

    with app.app_context():
        intocada = Receita(nome='Intocada Lote', categoria='X',
                           rendimento_qtd=1, rendimento_unidade='un',
                           peso_base=10.0, preco_loja=9.99)
        intocado_prod = Produto(nome='Intocado Prod Lote', categoria='Y',
                                preco_loja=77.77, ativo=True)
        db.session.add_all([intocada, intocado_prod])
        db.session.commit()
        rid, pid = intocada.id, intocado_prod.id

    client = app.test_client()
    _login(client, admin_user)
    token = _csrf(client)

    # POST vazio (so o CSRF)
    resp = client.post('/receitas/precos', data={'csrf_token': token},
                       follow_redirects=False)
    assert resp.status_code in (302, 303)

    with app.app_context():
        assert Receita.query.get(rid).preco_loja == 9.99
        assert Produto.query.get(pid).preco_loja == 77.77


def test_precos_lote_get_separa_cestas_de_produtos_simples(app, admin_user):
    """GET renderiza Produto com itens na secao 'Cestas' e sem itens em
    'Produtos simples'."""
    from app.extensions import db
    from app.models import Produto, ProdutoItem, Receita

    with app.app_context():
        r = Receita(nome='Base Cesta Get', categoria='Z',
                    rendimento_qtd=1, rendimento_unidade='un',
                    peso_base=10.0)
        simples = Produto(nome='ItemSimplesGet', categoria='Bebidas',
                          ativo=True)
        cesta = Produto(nome='BoxGet', categoria='Especiais', ativo=True)
        db.session.add_all([r, simples, cesta])
        db.session.commit()
        db.session.add(ProdutoItem(produto_id=cesta.id, tipo='receita',
                                    receita_id=r.id, item_nome='c',
                                    quantidade=1))
        db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/receitas/precos')
    assert resp.status_code == 200
    html = resp.data.decode()
    # Cesta aparece na secao Cestas (data-categoria="p:__cestas__")
    assert 'BoxGet' in html
    assert 'data-categoria="p:__cestas__"' in html
    # Simples aparece com sua categoria propria
    assert 'ItemSimplesGet' in html
    assert 'data-categoria="p:Bebidas"' in html

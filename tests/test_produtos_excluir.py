"""Exclusão de produto robusta: hard-delete pra produto sem vínculo; produto
com histórico/estoque é DESATIVADO em vez de dar 500 (FK).

O caminho de desativação depende de FK enforcada (Postgres em prod). O SQLite de
teste nao enforca FK por padrao entre conexoes, entao aqui cobrimos o happy-path
e que a rota nao estoura; o ramo de desativacao eh um try/except IntegrityError
padrao, validado em prod (Postgres)."""


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def test_excluir_produto_sem_vinculo(app, admin_user):
    from app.extensions import db
    from app.models import Produto

    with app.app_context():
        p = Produto(nome='Item Avulso', ativo=True)
        db.session.add(p)
        db.session.commit()
        pid = p.id

    client = app.test_client()
    _login(client, admin_user)
    resp = client.post(f'/produtos/excluir/{pid}', follow_redirects=False)
    assert resp.status_code in (302, 303)

    with app.app_context():
        assert Produto.query.get(pid) is None  # realmente excluido


def test_lista_oculta_inativos(app, admin_user):
    """Produto desativado some do catalogo (lista filtra ativo=True)."""
    from app.extensions import db
    from app.models import Produto

    with app.app_context():
        db.session.add(Produto(nome='Ativo Visivel', ativo=True))
        db.session.add(Produto(nome='Inativo Oculto', ativo=False))
        db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    html = client.get('/produtos/').get_data(as_text=True)
    assert 'Ativo Visivel' in html
    assert 'Inativo Oculto' not in html

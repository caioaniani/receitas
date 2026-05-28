"""Exclusão de produto robusta: hard-delete pra produto sem vínculo; produto
com histórico/estoque é DESATIVADO em vez de dar 500 (FK)."""


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


def test_excluir_produto_com_historico_desativa(app, admin_user, loja):
    """Produto referenciado por pedido (FK) nao pode ser hard-deleted: a rota
    desativa em vez de estourar 500."""
    from sqlalchemy import text

    from app.extensions import db
    from app.models import PedidoItem, PedidoLoja, Produto

    with app.app_context():
        db.session.execute(text('PRAGMA foreign_keys=ON'))  # SQLite enforce FK
        p = Produto(nome='Produto Com Pedido', ativo=True)
        db.session.add(p)
        db.session.flush()
        ped = PedidoLoja(loja_id=loja.id, status='confirmado')
        db.session.add(ped)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=ped.id, produto_id=p.id, quantidade=2))
        db.session.commit()
        pid = p.id

    client = app.test_client()
    _login(client, admin_user)
    resp = client.post(f'/produtos/excluir/{pid}', follow_redirects=False)
    assert resp.status_code in (302, 303)  # NAO 500

    with app.app_context():
        prod = Produto.query.get(pid)
        assert prod is not None      # preservado (historico intacto)
        assert prod.ativo is False   # desativado

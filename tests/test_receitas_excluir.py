"""Exclusão de receita robusta: deleta sem vínculo; se referenciada
(pedidos/estoque/produtos), bloqueia com mensagem em vez de 500 (FK).

O ramo de bloqueio depende de FK enforcada (Postgres em prod); o SQLite de teste
nao enforca FK entre conexoes, entao aqui cobrimos o happy-path e que a rota nao
estoura. O ramo eh um try/except IntegrityError padrao."""


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def test_excluir_receita_sem_vinculo(app, admin_user):
    from app.extensions import db
    from app.models import Receita

    with app.app_context():
        r = Receita(nome='Receita Avulsa', categoria='Teste', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=100.0)
        db.session.add(r)
        db.session.commit()
        rid = r.id

    client = app.test_client()
    _login(client, admin_user)
    resp = client.post(f'/receitas/{rid}/excluir', follow_redirects=False)
    assert resp.status_code in (302, 303)

    with app.app_context():
        assert Receita.query.get(rid) is None

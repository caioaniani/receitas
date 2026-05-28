"""Tela de saude do estoque — diagnostico de duplicidade (read-only).

Cobre os 3 sinais que a tela passou a mostrar:
- duplicata pura (mesmo item, mesmo estado, 2+ linhas);
- separacao por estado (mesmo item, estados distintos);
- cadastro homonimo no catalogo (nome repetido em Receita/Produto).
"""


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _receita(nome, categoria='Fornadas Especiais'):
    from app.models import Receita
    return Receita(nome=nome, categoria=categoria, rendimento_qtd=1,
                   rendimento_unidade='un', peso_base=100.0)


def test_saude_detecta_duplicata_pura(app, admin_user, loja):
    from app.extensions import db
    from app.models import EstoqueLoja

    with app.app_context():
        r = _receita('Danish de Calabresa')
        db.session.add(r)
        db.session.commit()
        rid, lid = r.id, loja.id
        for q in (1, 3, 1):  # mesma receita, estado None = duplicata pura
            db.session.add(EstoqueLoja(loja_id=lid, receita_id=rid,
                                       estado=None, quantidade=q))
        db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/pedidos/estoque-loja/saude')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'Itens duplicados no estoque' in html
    assert 'Danish de Calabresa' in html
    assert 'duplicata pura' in html


def test_saude_classifica_por_estado(app, admin_user, loja):
    from app.extensions import db
    from app.models import EstoqueLoja

    with app.app_context():
        r = _receita('Croissant', categoria='Viennoiserie')
        db.session.add(r)
        db.session.commit()
        rid, lid = r.id, loja.id
        db.session.add(EstoqueLoja(loja_id=lid, receita_id=rid,
                                   estado=None, quantidade=5))
        db.session.add(EstoqueLoja(loja_id=lid, receita_id=rid,
                                   estado='backup', quantidade=3))
        db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/pedidos/estoque-loja/saude')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'Croissant' in html
    assert 'por estado' in html


def test_saude_sem_duplicata_nao_alarma(app, admin_user, loja):
    from app.extensions import db
    from app.models import EstoqueLoja

    with app.app_context():
        r = _receita('Baguette Francesa')
        db.session.add(r)
        db.session.commit()
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=r.id,
                                   estado=None, quantidade=2))
        db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/pedidos/estoque-loja/saude')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'Nenhum item duplicado' in html


def test_saude_detecta_cadastro_homonimo(app, admin_user, loja):
    from app.extensions import db
    from app.models import Produto

    with app.app_context():
        db.session.add(_receita('Pao de Queijo', categoria='Paes'))
        db.session.add(_receita('Pao de Queijo', categoria='Paes'))  # homonimo
        db.session.add(Produto(nome='Pao de Queijo', ativo=True))  # colisao rec/prod
        db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/pedidos/estoque-loja/saude')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'Cadastros com nome repetido' in html
    assert 'Receita E Produto' in html
    assert 'Pao de Queijo' in html

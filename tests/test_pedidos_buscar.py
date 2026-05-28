"""Busca de itens por typeahead no novo pedido.

Cobre: endpoint /pedidos/buscar-itens.json retorna itens em formato
r_/p_/mp_, filtra por substring acento-insensível e multi-termo,
mínimo 2 caracteres, e está protegido por @pedidos_required.
"""


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def test_buscar_itens_receitas(app, admin_user):
    from app.extensions import db
    from app.models import Receita

    with app.app_context():
        r = Receita(nome='Pão Francês', categoria='Básicos',
                    rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0)
        db.session.add(r)
        db.session.commit()
        rid = r.id

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/pedidos/buscar-itens.json?q=pao')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'itens' in data
    nomes = [item['nome'] for item in data['itens']]
    assert 'Pão Francês' in nomes
    ids = [item['id'] for item in data['itens']]
    assert f'r_{rid}' in ids


def test_buscar_itens_produtos(app, admin_user):
    from app.extensions import db
    from app.models import Produto

    with app.app_context():
        p = Produto(nome='Cesta Especial', ativo=True)
        db.session.add(p)
        db.session.commit()
        pid = p.id

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/pedidos/buscar-itens.json?q=cesta')
    assert resp.status_code == 200
    data = resp.get_json()
    nomes = [item['nome'] for item in data['itens']]
    assert 'Cesta Especial' in nomes
    ids = [item['id'] for item in data['itens']]
    assert f'p_{pid}' in ids


def test_buscar_itens_materias_primas(app, admin_user):
    from app.extensions import db
    from app.models import MateriaPrima

    with app.app_context():
        m = MateriaPrima(nome='Farinha Integral', unidade='kg', custo_por_kg=5.0)
        db.session.add(m)
        db.session.commit()
        mid = m.id

    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/pedidos/buscar-itens.json?q=farinha')
    assert resp.status_code == 200
    data = resp.get_json()
    nomes = [item['nome'] for item in data['itens']]
    assert 'Farinha Integral' in nomes
    ids = [item['id'] for item in data['itens']]
    assert f'mp_{mid}' in ids


def test_buscar_itens_acento_insensivel(app, admin_user):
    from app.extensions import db
    from app.models import Receita

    with app.app_context():
        r = Receita(nome='Pão de Queijo', categoria='Pães',
                    rendimento_qtd=1, rendimento_unidade='un', peso_base=50.0)
        db.session.add(r)
        db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    # Busca sem acento mas receita tem
    resp = client.get('/pedidos/buscar-itens.json?q=pao')
    assert resp.status_code == 200
    data = resp.get_json()
    nomes = [item['nome'] for item in data['itens']]
    assert 'Pão de Queijo' in nomes


def test_buscar_itens_multi_termo(app, admin_user):
    from app.extensions import db
    from app.models import Receita

    with app.app_context():
        r1 = Receita(nome='Pão de Queijo', categoria='Pães',
                     rendimento_qtd=1, rendimento_unidade='un', peso_base=50.0)
        r2 = Receita(nome='Pão Francês', categoria='Pães',
                     rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0)
        db.session.add(r1)
        db.session.add(r2)
        db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    # Busca por múltiplos termos
    resp = client.get('/pedidos/buscar-itens.json?q=pao queijo')
    assert resp.status_code == 200
    data = resp.get_json()
    nomes = [item['nome'] for item in data['itens']]
    # Só "Pão de Queijo" casa com ambos
    assert 'Pão de Queijo' in nomes
    assert 'Pão Francês' not in nomes


def test_buscar_itens_minimo_2_chars(app, admin_user):
    from app.extensions import db
    from app.models import Receita

    with app.app_context():
        r = Receita(nome='Pão Francês', categoria='Pães',
                    rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0)
        db.session.add(r)
        db.session.commit()

    client = app.test_client()
    _login(client, admin_user)
    # Busca com menos de 2 caracteres
    resp = client.get('/pedidos/buscar-itens.json?q=p')
    assert resp.status_code == 200
    data = resp.get_json()
    # Deve retornar vazio
    assert len(data['itens']) == 0


def test_buscar_itens_padeiro_bloqueado(app):
    from app.extensions import db
    from app.models import Receita, Usuario

    with app.app_context():
        padeiro = Usuario(login='padeiro', nome='Padeiro', papel='padeiro')
        padeiro.set_senha('senha123')
        db.session.add(padeiro)
        r = Receita(nome='Pão Francês', categoria='Pães',
                    rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0)
        db.session.add(r)
        db.session.commit()

    client = app.test_client()
    _login(client, padeiro)
    # Padeiro não tem acesso
    resp = client.get('/pedidos/buscar-itens.json?q=pao')
    assert resp.status_code == 403

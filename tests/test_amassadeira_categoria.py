"""Config da amassadeira POR CATEGORIA (/receitas/amassadeira).

Aplica uma capacidade a todas as receitas da categoria; 0 = nao usa; campo
em branco nao altera.
"""
from app.extensions import db
from app.models import Receita


def _rec(nome, categoria, cap=50000):
    r = Receita(nome=nome, categoria=categoria, rendimento_qtd=1,
                rendimento_unidade='un', peso_base=1000.0,
                capacidade_amassadeira_g=cap)
    db.session.add(r)
    db.session.commit()
    return r


def _login(client, admin_user):
    client.post('/auth/login',
                data={'login': admin_user.login, 'senha': '123'},
                follow_redirects=True)


def test_get_lista_categorias(app, admin_user):
    _rec('Croissant A', 'Croissants')
    _rec('Moeda Vermelha', 'Moedas')
    client = app.test_client()
    _login(client, admin_user)
    resp = client.get('/receitas/amassadeira')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Croissants' in body
    assert 'Moedas' in body


def test_aplica_por_categoria(app, admin_user):
    r1 = _rec('Croissant A', 'Croissants')
    r2 = _rec('Croissant B', 'Croissants')
    m = _rec('Moeda Vermelha', 'Moedas')

    client = app.test_client()
    _login(client, admin_user)
    resp = client.post('/receitas/amassadeira', data={
        'categoria_0': 'Croissants', 'cap_0': '40000',
        'categoria_1': 'Moedas', 'cap_1': '0',
    })
    assert resp.status_code == 302

    for r in (r1, r2, m):
        db.session.refresh(r)
    assert r1.capacidade_amassadeira_g == 40000
    assert r2.capacidade_amassadeira_g == 40000   # toda a categoria
    assert m.capacidade_amassadeira_g == 0        # Moedas nao usa amassadeira


def test_branco_nao_altera(app, admin_user):
    r = _rec('Pão', 'Paes', cap=50000)
    client = app.test_client()
    _login(client, admin_user)
    client.post('/receitas/amassadeira',
                data={'categoria_0': 'Paes', 'cap_0': ''})
    db.session.refresh(r)
    assert r.capacidade_amassadeira_g == 50000    # inalterado

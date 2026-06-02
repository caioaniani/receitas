"""Cadastro de modo_preparo em lote (admin only).

Bug que isso evita: a tela em lote precisa filtrar pendentes corretamente
(textos vazios contam como pendentes, nao so NULLs) e o auto-save por
textarea precisa funcionar sem mexer no resto da receita.
"""


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)
        sess['_fresh'] = True


def _criar_receitas(app):
    """3 receitas: 1 vazia, 1 com texto, 1 com string vazia (NULL)."""
    from app.extensions import db
    from app.models import Receita
    with app.app_context():
        r1 = Receita(nome='Pao Frances', categoria='Paes',
                     rendimento_qtd=10, rendimento_unidade='un',
                     peso_base=1000.0)
        r2 = Receita(nome='Croissant', categoria='Croissants',
                     rendimento_qtd=8, rendimento_unidade='un',
                     peso_base=800.0, modo_preparo='Passo 1: misturar.')
        r3 = Receita(nome='Brioche', categoria='Paes',
                     rendimento_qtd=6, rendimento_unidade='un',
                     peso_base=600.0, modo_preparo='')
        db.session.add_all([r1, r2, r3])
        db.session.commit()
        return {'vazia': r1.id, 'cheia': r2.id, 'vazia_str': r3.id}


def test_index_filtro_pendentes_e_contagem(app, admin_user):
    ids = _criar_receitas(app)
    client = app.test_client()
    _login(client, admin_user.id)
    r = client.get('/receitas/modos-preparo')
    assert r.status_code == 200
    html = r.data.decode('utf-8')
    # contagem global: 3 total, 1 preenchida
    assert '1</strong> de <strong>3' in html
    # filtro default = pendentes: aparecem a vazia e a vazia_str, mas NAO a cheia
    assert 'Pao Frances' in html
    assert 'Brioche' in html
    assert 'Croissant' not in html


def test_index_filtro_preenchidas(app, admin_user):
    _criar_receitas(app)
    client = app.test_client()
    _login(client, admin_user.id)
    r = client.get('/receitas/modos-preparo?filtro=preenchidas')
    assert r.status_code == 200
    html = r.data.decode('utf-8')
    assert 'Croissant' in html
    # texto atual aparece dentro do textarea
    assert 'Passo 1: misturar.' in html
    assert 'Pao Frances' not in html


def test_salvar_json_grava_modo_preparo(app, admin_user):
    from app.models import Receita
    ids = _criar_receitas(app)
    client = app.test_client()
    _login(client, admin_user.id)
    r = client.post('/receitas/modos-preparo/salvar.json', data={
        'receita_id': ids['vazia'],
        'texto': 'Misturar farinha e agua. Sovar 10min. Forno 220C.',
    })
    assert r.status_code == 200 and r.get_json()['ok']
    with app.app_context():
        receita = Receita.query.get(ids['vazia'])
        assert receita.modo_preparo == 'Misturar farinha e agua. Sovar 10min. Forno 220C.'
    # apaga voltando string vazia: vira NULL
    r2 = client.post('/receitas/modos-preparo/salvar.json', data={
        'receita_id': ids['vazia'],
        'texto': '   ',
    })
    assert r2.status_code == 200 and r2.get_json()['ok']
    with app.app_context():
        assert Receita.query.get(ids['vazia']).modo_preparo is None


def test_salvar_json_receita_inexistente_404(app, admin_user):
    _criar_receitas(app)
    client = app.test_client()
    _login(client, admin_user.id)
    r = client.post('/receitas/modos-preparo/salvar.json', data={
        'receita_id': 999999, 'texto': 'qualquer',
    })
    assert r.status_code == 404
    assert not r.get_json()['ok']


def test_salvar_json_403_pra_nao_admin(app):
    """Gerente comum NAO pode editar modo_preparo via tela em lote."""
    from app.extensions import db
    from app.models import Loja, Receita, Usuario
    with app.app_context():
        loja = Loja(nome='Loja X', ativa=True)
        db.session.add(loja)
        db.session.flush()
        gerente = Usuario(nome='Gerente', login='ger', papel='gerente',
                          loja_id=loja.id)
        gerente.set_senha('x')
        r = Receita(nome='Pao Test', categoria='Paes',
                    rendimento_qtd=1, rendimento_unidade='un',
                    peso_base=100.0)
        db.session.add_all([gerente, r])
        db.session.commit()
        gid = gerente.id
        rid = r.id
    client = app.test_client()
    _login(client, gid)
    # index
    assert client.get('/receitas/modos-preparo').status_code == 403
    # salvar.json
    s = client.post('/receitas/modos-preparo/salvar.json', data={
        'receita_id': rid, 'texto': 'tentativa',
    })
    assert s.status_code == 403

"""Diagnostico de nomes duplicados no catalogo (colisao do typeahead de vinculo).

Nome de Receita/Produto/MateriaPrima nao e unico no banco; nomes iguais colidem
no campo de busca de vinculo. A tela /pedidos/catalogo/nomes-duplicados lista
isso pra limpeza, marcando as colisoes EXATAS (mesmo rotulo no typeahead).
"""


def _rec(db, nome, cat=None):
    from app.models import Receita
    r = Receita(nome=nome, categoria=cat, rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.commit()
    return r


def _prod(db, nome, ativo=True):
    from app.models import Produto
    p = Produto(nome=nome, ativo=ativo)
    db.session.add(p)
    db.session.commit()
    return p


def test_helper_agrupa_e_marca_colisao_exata(app):
    from app.blueprints.pedidos.routes import _grupos_nomes_duplicados
    from app.extensions import db
    from app.models import Receita
    with app.app_context():
        # exato: mesmo nome E mesma categoria -> colisao no typeahead
        _rec(db, 'Pão de Forma', None)
        _rec(db, 'Pão de Forma', None)
        # mesmo nome, categoria diferente -> agrupa, mas NAO e colisao exata
        _rec(db, 'Bolo', 'Doces')
        _rec(db, 'Bolo', 'Festa')
        # case diferente -> agrupa (case-insensitive), nao e colisao exata
        _rec(db, 'Brioche', None)
        _rec(db, 'brioche', None)
        # unico -> nao aparece
        _rec(db, 'Sourdough', None)

        grupos = _grupos_nomes_duplicados(
            Receita.query.order_by(Receita.nome).all(), tem_categoria=True)
        por_nome = {g['nome'].lower(): g for g in grupos}

        assert 'sourdough' not in por_nome              # unico fica de fora
        assert por_nome['pão de forma']['n'] == 2
        assert por_nome['pão de forma']['colisao_exata'] is True
        assert por_nome['bolo']['colisao_exata'] is False   # categoria difere
        assert por_nome['brioche']['colisao_exata'] is False  # so o case difere
        # ordena por nº de repeticoes desc
        assert grupos[0]['n'] >= grupos[-1]['n']


def test_produto_e_mp_duplicados(app):
    from app.blueprints.pedidos.routes import _grupos_nomes_duplicados
    from app.extensions import db
    from app.models import MateriaPrima, Produto
    with app.app_context():
        _prod(db, 'Cookie')
        _prod(db, 'Cookie')
        _prod(db, 'Unico')
        db.session.add_all([MateriaPrima(nome='Manteiga', unidade='kg', custo_por_kg=1),
                            MateriaPrima(nome='Manteiga', unidade='kg', custo_por_kg=1)])
        db.session.commit()
        gp = _grupos_nomes_duplicados(Produto.query.all())
        gm = _grupos_nomes_duplicados(MateriaPrima.query.all())
        assert len(gp) == 1 and gp[0]['nome'] == 'Cookie' and gp[0]['colisao_exata'] is True
        assert len(gm) == 1 and gm[0]['nome'] == 'Manteiga'


def test_rota_lista_duplicados(app, admin_user):
    from app.extensions import db
    with app.app_context():
        _rec(db, 'Pão de Forma', None)
        _rec(db, 'Pão de Forma', None)
        c = app.test_client()
        c.post('/auth/login', data={'login': admin_user.login, 'senha': '123'},
               follow_redirects=True)
        r = c.get('/pedidos/catalogo/nomes-duplicados')
        assert r.status_code == 200
        h = r.get_data(as_text=True)
        assert 'Pão de Forma' in h
        assert 'colisão exata' in h

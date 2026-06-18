"""Ordenação manual de produtos e categorias na vitrine (17/06/2026).

- `ordem_site` em Produto/Receita: menor = mais cedo; NULL = fim alfabético.
- `CategoriaSite(nome, ordem)`: ordem das categorias na vitrine.
"""


def _owner(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Dono', login='dono', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def test_ordem_site_respeitada_no_listing(app):
    """Item com ordem_site=1 vem antes do ordem_site=2; null vai pro fim."""
    from app.extensions import db
    from app.models import Produto
    from app.services import loja_catalogo
    with app.app_context():
        for nome, ordem in [('Z', 1), ('A', 2), ('M', None)]:
            db.session.add(Produto(nome=nome, preco_site=10.0,
                                   categoria='Pães',
                                   ordem_site=ordem,
                                   imagem_dropbox_url='https://x/y.jpg',
                                   ativo=True))
        db.session.commit()
        itens = [i['nome'] for i in loja_catalogo.produtos_publicados()]
    # Z (ord 1) antes de A (ord 2). M (null) por último.
    assert itens.index('Z') < itens.index('A') < itens.index('M')


def test_categorias_ordenadas_por_categoria_site(app):
    """A tabela CategoriaSite manda na ordem dos grupos na vitrine."""
    from app.extensions import db
    from app.models import CategoriaSite, Produto
    from app.services import loja_catalogo
    with app.app_context():
        db.session.add(Produto(nome='Café', preco_site=8.0, categoria='Bebidas',
                               imagem_dropbox_url='https://x/c.jpg', ativo=True))
        db.session.add(Produto(nome='Pão', preco_site=10.0, categoria='Pães',
                               imagem_dropbox_url='https://x/p.jpg', ativo=True))
        db.session.add(Produto(nome='Geleia', preco_site=12.0,
                               categoria='Conservas',
                               imagem_dropbox_url='https://x/g.jpg', ativo=True))
        # Pesos: Pães primeiro, Bebidas segundo. Conservas sem peso (fim).
        db.session.add(CategoriaSite(nome='Pães', ordem=1))
        db.session.add(CategoriaSite(nome='Bebidas', ordem=2))
        db.session.commit()
        grupos = loja_catalogo.por_categorias(
            loja_catalogo.produtos_publicados())
    nomes_cats = [c for c, _ in grupos]
    assert nomes_cats.index('Pães') < nomes_cats.index('Bebidas') \
        < nomes_cats.index('Conservas')


def test_endpoint_atualiza_ordem_produto(app):
    from app.extensions import db
    from app.models import Produto
    c = _owner(app)
    with app.app_context():
        p = Produto(nome='X', categoria='Pães', preco_site=10.0, ativo=True)
        db.session.add(p)
        db.session.commit()
        pid = p.id
    r = c.post(f'/admin/loja-online/catalogo/ordem/produto/{pid}',
                json={'ordem': 5})
    assert r.status_code == 200 and r.get_json()['ok']
    with app.app_context():
        assert Produto.query.get(pid).ordem_site == 5


def test_endpoint_ordem_null_limpa(app):
    from app.extensions import db
    from app.models import Produto
    c = _owner(app)
    with app.app_context():
        p = Produto(nome='X', categoria='Pães', preco_site=10.0, ativo=True,
                    ordem_site=99)
        db.session.add(p)
        db.session.commit()
        pid = p.id
    c.post(f'/admin/loja-online/catalogo/ordem/produto/{pid}',
            json={'ordem': None})
    with app.app_context():
        assert Produto.query.get(pid).ordem_site is None


def test_endpoint_ordem_invalida_rejeita(app):
    from app.extensions import db
    from app.models import Produto
    c = _owner(app)
    with app.app_context():
        p = Produto(nome='X', categoria='Pães', preco_site=10.0, ativo=True)
        db.session.add(p)
        db.session.commit()
        pid = p.id
    r = c.post(f'/admin/loja-online/catalogo/ordem/produto/{pid}',
                json={'ordem': 'abc'})
    assert r.status_code == 400


def test_pagina_categorias_lista_em_uso(app):
    from app.extensions import db
    from app.models import Produto
    c = _owner(app)
    with app.app_context():
        db.session.add(Produto(nome='X', categoria='Pães',
                               preco_site=10.0, ativo=True))
        db.session.add(Produto(nome='Y', categoria='Bebidas',
                               preco_site=8.0, ativo=True))
        db.session.commit()
    r = c.get('/admin/loja-online/categorias')
    assert r.status_code == 200
    assert b'P\xc3\xa3es' in r.data   # "Pães" UTF-8
    assert b'Bebidas' in r.data


def test_pagina_categorias_salva_ordem(app):
    from werkzeug.datastructures import MultiDict

    from app.models import CategoriaSite
    c = _owner(app)
    r = c.post('/admin/loja-online/categorias',
                data=MultiDict([
                    ('nome', 'Pães'), ('ordem', '1'),
                    ('nome', 'Bebidas'), ('ordem', '2'),
                ]))
    assert r.status_code == 302
    with app.app_context():
        cats = {c.nome: c.ordem for c in CategoriaSite.query.all()}
        assert cats == {'Pães': 1, 'Bebidas': 2}


def test_categorias_repost_atualiza(app):
    """POST com a mesma categoria de novo upserta (não cria duplicata)."""
    from werkzeug.datastructures import MultiDict

    from app.models import CategoriaSite
    c = _owner(app)
    c.post('/admin/loja-online/categorias',
            data=MultiDict([('nome', 'Pães'), ('ordem', '1')]))
    c.post('/admin/loja-online/categorias',
            data=MultiDict([('nome', 'Pães'), ('ordem', '5')]))
    with app.app_context():
        regs = CategoriaSite.query.filter_by(nome='Pães').all()
        assert len(regs) == 1 and regs[0].ordem == 5


def test_tela_ordem_produtos_lista_agrupada(app):
    """A tela /admin/loja-online/ordem-produtos mostra os itens agrupados
    por categoria, prontos pra arrastar."""
    from app.extensions import db
    from app.models import Produto
    c = _owner(app)
    with app.app_context():
        db.session.add(Produto(nome='Pão', categoria='Pães',
                               preco_site=10.0, ativo=True,
                               imagem_dropbox_url='https://x/p.jpg'))
        db.session.add(Produto(nome='Café', categoria='Bebidas',
                               preco_site=8.0, ativo=True,
                               imagem_dropbox_url='https://x/c.jpg'))
        db.session.commit()
    r = c.get('/admin/loja-online/ordem-produtos')
    assert r.status_code == 200
    assert b'prod-list' in r.data    # listas sortable
    assert b'P\xc3\xa3o' in r.data   # "Pão" UTF-8
    assert b'Caf\xc3\xa9' in r.data  # "Café" UTF-8


def test_endpoint_ordem_categorias_em_lote(app):
    """POST com lista de nomes salva ordem (índice = ordem)."""
    from app.models import CategoriaSite
    c = _owner(app)
    r = c.post('/admin/loja-online/categorias/ordem',
                json={'ordem': ['Pães', 'Bebidas', 'Conservas']})
    assert r.status_code == 200 and r.get_json()['ok'] is True
    with app.app_context():
        cats = {c.nome: c.ordem for c in CategoriaSite.query.all()}
        assert cats == {'Pães': 0, 'Bebidas': 1, 'Conservas': 2}


def test_endpoint_ordem_categorias_repete_upsert(app):
    """Segunda chamada com a mesma categoria atualiza, não duplica."""
    from app.models import CategoriaSite
    c = _owner(app)
    c.post('/admin/loja-online/categorias/ordem',
            json={'ordem': ['Pães']})
    c.post('/admin/loja-online/categorias/ordem',
            json={'ordem': ['Bebidas', 'Pães']})
    with app.app_context():
        cats = {c.nome: c.ordem for c in CategoriaSite.query.all()}
        # 'Bebidas' foi pra ordem 0, 'Pães' foi pra ordem 1 (atualizou)
        assert cats == {'Bebidas': 0, 'Pães': 1}


def test_endpoint_ordem_produtos_em_lote(app):
    """POST com lista de itens [{tipo, id}] salva ordem_site = índice."""
    from app.extensions import db
    from app.models import Produto, Receita
    c = _owner(app)
    with app.app_context():
        p1 = Produto(nome='A', categoria='Pães', preco_site=10.0, ativo=True)
        p2 = Produto(nome='B', categoria='Pães', preco_site=10.0, ativo=True)
        r1 = Receita(nome='C', categoria='Pães', preco_site=10.0,
                      rendimento_qtd=1, rendimento_unidade='un', peso_base=100.0)
        db.session.add_all([p1, p2, r1])
        db.session.commit()
        ids = {'p1': p1.id, 'p2': p2.id, 'r1': r1.id}
    r = c.post('/admin/loja-online/produtos/ordem',
                json={'itens': [
                    {'tipo': 'produto', 'id': ids['p2']},
                    {'tipo': 'receita', 'id': ids['r1']},
                    {'tipo': 'produto', 'id': ids['p1']},
                ]})
    assert r.status_code == 200 and r.get_json()['ok'] is True
    with app.app_context():
        assert Produto.query.get(ids['p2']).ordem_site == 0
        assert Receita.query.get(ids['r1']).ordem_site == 1
        assert Produto.query.get(ids['p1']).ordem_site == 2


def test_endpoint_ordem_produtos_ignora_id_inexistente(app):
    """Item com id inexistente é ignorado (não estoura)."""
    c = _owner(app)
    r = c.post('/admin/loja-online/produtos/ordem',
                json={'itens': [{'tipo': 'produto', 'id': 99999}]})
    assert r.status_code == 200


def test_endpoint_ordem_produtos_payload_invalido_rejeita(app):
    c = _owner(app)
    r = c.post('/admin/loja-online/produtos/ordem',
                json={'itens': 'string'})
    assert r.status_code == 400


def test_endpoint_ordem_categorias_payload_invalido_rejeita(app):
    c = _owner(app)
    r = c.post('/admin/loja-online/categorias/ordem',
                json={'ordem': 'string'})
    assert r.status_code == 400


def test_endpoints_ordem_em_lote_exigem_owner(app):
    c = app.test_client()
    r1 = c.post('/admin/loja-online/categorias/ordem',
                 json={'ordem': []})
    r2 = c.post('/admin/loja-online/produtos/ordem',
                 json={'itens': []})
    r3 = c.get('/admin/loja-online/ordem-produtos')
    assert r1.status_code in (302, 401, 403)
    assert r2.status_code in (302, 401, 403)
    assert r3.status_code in (302, 401, 403)


def test_endpoints_exigem_owner(app):
    c = app.test_client()
    r1 = c.post('/admin/loja-online/catalogo/ordem/produto/1',
                json={'ordem': 1})
    r2 = c.get('/admin/loja-online/categorias')
    assert r1.status_code in (302, 401, 403)
    assert r2.status_code in (302, 401, 403)

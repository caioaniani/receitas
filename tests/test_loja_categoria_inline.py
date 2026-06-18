"""Fallback de categoria + edição inline (17/06/2026).

Bug: `Produto` sem categoria caía em 'Cestas' automático → geleia/molho/
qualquer Produto não-cesta aparecia em Cestas na vitrine. Fix:
- Fallback agora é 'Outros' (não 'Cestas').
- Tela de curadoria ganhou input de categoria com salvar inline e
  autocomplete via <datalist>.
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


def test_produto_sem_categoria_cai_em_outros_nao_em_cestas(app):
    """Regressão: Produto sem `categoria` definida NÃO deve aparecer em
    Cestas — vai pra 'Outros'."""
    from app.extensions import db
    from app.models import Produto
    from app.services import loja_catalogo
    with app.app_context():
        p = Produto(nome='Geleia de Morango', preco_site=15.0,
                    categoria=None,  # SEM categoria
                    imagem_dropbox_url='https://x/g.jpg', ativo=True)
        db.session.add(p)
        db.session.commit()
        itens = loja_catalogo.produtos_publicados()
    ger = [i for i in itens if i['nome'] == 'Geleia de Morango']
    assert ger and ger[0]['categoria'] == 'Outros'


def test_produto_com_categoria_explicita_respeita(app):
    """Produto COM categoria cadastrada continua nela (não vai pra Outros)."""
    from app.extensions import db
    from app.models import Produto
    from app.services import loja_catalogo
    with app.app_context():
        p = Produto(nome='Box Mimo', preco_site=100.0, categoria='Cestas',
                    imagem_dropbox_url='https://x/b.jpg', ativo=True)
        db.session.add(p)
        db.session.commit()
        itens = loja_catalogo.produtos_publicados()
    box = [i for i in itens if i['nome'] == 'Box Mimo']
    assert box and box[0]['categoria'] == 'Cestas'


def test_atualizar_categoria_produto(app):
    """POST /admin/loja-online/catalogo/categoria/produto/<id> grava."""
    from app.extensions import db
    from app.models import Produto
    c = _owner(app)
    with app.app_context():
        p = Produto(nome='Geleia X', categoria=None, preco_site=10.0,
                    ativo=True)
        db.session.add(p)
        db.session.commit()
        pid = p.id
    r = c.post(f'/admin/loja-online/catalogo/categoria/produto/{pid}',
                json={'categoria': 'Conservas'})
    assert r.status_code == 200
    assert r.get_json()['ok'] is True
    with app.app_context():
        assert Produto.query.get(pid).categoria == 'Conservas'


def test_atualizar_categoria_receita(app):
    from app.extensions import db
    from app.models import Receita
    c = _owner(app)
    with app.app_context():
        rec = Receita(nome='Sourdough', categoria='Pães', preco_site=20.0,
                      rendimento_qtd=1, rendimento_unidade='un',
                      peso_base=100.0)
        db.session.add(rec)
        db.session.commit()
        rid = rec.id
    r = c.post(f'/admin/loja-online/catalogo/categoria/receita/{rid}',
                json={'categoria': 'Pães Especiais'})
    assert r.status_code == 200
    with app.app_context():
        assert Receita.query.get(rid).categoria == 'Pães Especiais'


def test_categoria_vazia_volta_pra_null(app):
    """Mandar categoria vazia limpa o campo (cai em Outros na vitrine)."""
    from app.extensions import db
    from app.models import Produto
    c = _owner(app)
    with app.app_context():
        p = Produto(nome='X', categoria='Conservas', preco_site=10.0,
                    ativo=True)
        db.session.add(p)
        db.session.commit()
        pid = p.id
    c.post(f'/admin/loja-online/catalogo/categoria/produto/{pid}',
            json={'categoria': '  '})
    with app.app_context():
        assert Produto.query.get(pid).categoria is None


def test_curadoria_tem_input_categoria_e_datalist(app):
    """A tela de curadoria expõe input de categoria + datalist de
    sugestões (categorias já existentes)."""
    from app.extensions import db
    from app.models import Produto
    c = _owner(app)
    with app.app_context():
        db.session.add(Produto(nome='A', categoria='Cestas',
                                preco_site=10.0, ativo=True))
        db.session.add(Produto(nome='B', categoria='Conservas',
                                preco_site=15.0, ativo=True))
        db.session.commit()
    r = c.get('/admin/loja-online/catalogo')
    assert r.status_code == 200
    assert b'categoria-input' in r.data
    assert b'categorias-existentes' in r.data
    # As duas categorias aparecem no datalist
    assert b'Cestas' in r.data
    assert b'Conservas' in r.data


def test_categoria_endpoint_exige_owner(app):
    c = app.test_client()
    r = c.post('/admin/loja-online/catalogo/categoria/produto/1',
                json={'categoria': 'X'})
    assert r.status_code in (302, 401, 403)


def test_categoria_tipo_invalido_rejeita(app):
    c = _owner(app)
    r = c.post('/admin/loja-online/catalogo/categoria/cesta/1',
                json={'categoria': 'X'})
    assert r.status_code == 400

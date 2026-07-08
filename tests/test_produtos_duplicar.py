"""Duplicar produto (POST /produtos/<id>/duplicar).

Copia cadastro + composição inteira (com FK — item_nome é só fallback).
NÃO copia de propósito:
- preco_site/ordem_site: publicação na vitrine é preco_site > 0
  (loja_catalogo.produtos_publicados) — a cópia não pode ir pro site
  sem revisão;
- imagem_*: remover imagem deleta o arquivo do Dropbox pelo storage_path
  — cópia compartilhando o arquivo perderia a imagem dos dois.
"""


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _produto_cafe(db):
    """Produto composto estilo 'Café Expresso': MP fracionária + 0.2 receita."""
    from app.models import MateriaPrima, Produto, ProdutoItem, Receita

    cookie = Receita(nome='Cookie Calebaut Dup', categoria='Doces',
                     rendimento_qtd=1, rendimento_unidade='un',
                     peso_base=100.0)
    graos = MateriaPrima(nome='Grãos de Café Dup', unidade='kg',
                         custo_por_kg=60.0)
    db.session.add_all([cookie, graos])
    db.session.flush()

    p = Produto(
        nome='Café Expresso Dup',
        categoria='Cafés',
        descricao='expresso com cortesia',
        preco_loja=9.0,
        preco_site=12.0,
        ordem_site=3,
        preco_interno=5.0,
        custo_embalagem=0.5,
        observacao='obs interna',
        imagem_dropbox_url='https://dropbox/x?raw=1',
        imagem_storage_path='/cardapio/x.jpg',
        ativo=True,
    )
    db.session.add(p)
    db.session.flush()
    db.session.add(ProdutoItem(produto_id=p.id, tipo='receita',
                               receita_id=cookie.id,
                               item_nome=cookie.nome, quantidade=0.2))
    db.session.add(ProdutoItem(produto_id=p.id, tipo='mp',
                               materia_prima_id=graos.id,
                               item_nome=graos.nome, quantidade=0.008))
    db.session.commit()
    return p, cookie, graos


def test_duplicar_copia_composicao_com_fk(app, admin_user):
    from app.extensions import db
    from app.models import Produto

    with app.app_context():
        p, cookie, graos = _produto_cafe(db)
        pid, cookie_id, graos_id = p.id, cookie.id, graos.id

    client = app.test_client()
    _login(client, admin_user)
    resp = client.post(f'/produtos/{pid}/duplicar', follow_redirects=False)
    assert resp.status_code in (302, 303)

    with app.app_context():
        copia = Produto.query.filter_by(nome='Cópia de Café Expresso Dup').first()
        assert copia is not None
        assert copia.id != pid
        # cadastro copiado
        assert copia.categoria == 'Cafés'
        assert copia.preco_loja == 9.0
        assert copia.preco_interno == 5.0
        assert copia.custo_embalagem == 0.5
        assert copia.observacao == 'obs interna'
        assert copia.ativo is True
        # composição inteira, com FK e fração preservadas
        assert len(copia.itens) == 2
        por_tipo = {it.tipo: it for it in copia.itens}
        assert por_tipo['receita'].receita_id == cookie_id
        assert por_tipo['receita'].quantidade == 0.2
        assert por_tipo['mp'].materia_prima_id == graos_id
        assert por_tipo['mp'].quantidade == 0.008


def test_duplicar_nao_copia_site_nem_imagem(app, admin_user):
    from app.extensions import db
    from app.models import Produto

    with app.app_context():
        p, _, _ = _produto_cafe(db)
        pid = p.id

    client = app.test_client()
    _login(client, admin_user)
    client.post(f'/produtos/{pid}/duplicar')

    with app.app_context():
        copia = Produto.query.filter_by(nome='Cópia de Café Expresso Dup').first()
        # vitrine: publicação é preco_site > 0 — cópia NÃO pode nascer publicada
        assert copia.preco_site is None
        assert copia.ordem_site is None
        # imagem: storage_path compartilhado quebraria a remoção (delete Dropbox)
        assert copia.imagem_dropbox_url is None
        assert copia.imagem_storage_path is None
        assert copia.imagem_url is None


def test_duplicar_exige_admin(app):
    from app.extensions import db
    from app.models import Usuario

    with app.app_context():
        p, _, _ = _produto_cafe(db)
        pid = p.id
        func = Usuario(nome='func teste', login='func-dup', papel='funcionario')
        func.set_senha('123')
        db.session.add(func)
        db.session.commit()

    client = app.test_client()
    _login(client, func)
    resp = client.post(f'/produtos/{pid}/duplicar', follow_redirects=False)
    assert resp.status_code in (302, 303, 403)
    with app.app_context():
        from app.models import Produto
        assert Produto.query.filter_by(
            nome='Cópia de Café Expresso Dup').first() is None

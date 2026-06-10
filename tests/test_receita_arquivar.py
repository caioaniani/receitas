"""Arquivamento de receita: caminho pra receita com histórico (pedidos,
vendas, estoque) que não pode ser excluída — sai das listas e seletores,
histórico 100% preservado. Toggle reversível."""


def _login(c):
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def _receita(app, nome, **kw):
    from app.extensions import db
    from app.models import Receita
    with app.app_context():
        r = Receita(nome=nome, categoria='Paes', rendimento_qtd=10,
                    rendimento_unidade='un', peso_base=1000.0, **kw)
        db.session.add(r)
        db.session.commit()
        return r.id


def test_arquivar_toggle_e_listas(app, admin_user):
    from app.extensions import db
    from app.models import Receita
    rid = _receita(app, 'Molho Antigo')
    c = app.test_client()
    _login(c)

    assert c.post(f'/receitas/{rid}/arquivar').status_code == 302
    with app.app_context():
        r = db.session.get(Receita, rid)
        assert r.arquivada_em is not None
        assert r.arquivada_por_id is not None

    # some dos cards do padeiro, aparece na secao de arquivadas
    pg = c.get('/receitas/padeiro')
    assert b'Arquivadas (1)' in pg.data
    assert pg.data.count(b'Molho Antigo') == 1   # so o badge da secao

    # banner + desarquivar na ficha
    ficha = c.get(f'/receitas/{rid}')
    assert 'Receita arquivada'.encode() in ficha.data

    assert c.post(f'/receitas/{rid}/arquivar').status_code == 302  # toggle
    with app.app_context():
        assert db.session.get(Receita, rid).arquivada_em is None
    pg2 = c.get('/receitas/padeiro')
    assert b'Arquivadas (' not in pg2.data


def test_copilot_resolver_ignora_arquivada(app, admin_user):
    from app.extensions import db
    from app.models import Receita
    from app.services.copilot import _resolver_receita_produto
    rid = _receita(app, 'Croissant Especial')
    with app.app_context():
        assert _resolver_receita_produto('Croissant Especial') is not None
        r = db.session.get(Receita, rid)
        from app.utils import agora
        r.arquivada_em = agora()
        db.session.commit()
        assert _resolver_receita_produto('Croissant Especial') is None


def test_precos_post_nao_zera_arquivada(app, admin_user):
    """Regressao da filtragem: a tela de precos so lista ativas — o POST nao
    pode zerar os precos das arquivadas (ausentes do form)."""
    from app.extensions import db
    from app.models import Receita
    from app.utils import agora
    rid_a = _receita(app, 'Ativa', preco_loja=5.0)
    rid_q = _receita(app, 'Arquivada', preco_loja=10.0)
    with app.app_context():
        db.session.get(Receita, rid_q).arquivada_em = agora()
        db.session.commit()

    c = app.test_client()
    _login(c)
    r = c.post('/receitas/precos', data={f'preco_loja_{rid_a}': '6,50',
                                         f'preco_site_{rid_a}': '',
                                         f'preco_venda_{rid_a}': ''},
               follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        assert db.session.get(Receita, rid_a).preco_loja == 6.5
        assert db.session.get(Receita, rid_q).preco_loja == 10.0  # intacto

"""Estoque na vitrine: NADA some, esgotado aparece com selo (18/06/2026).

Regra do dono: todo produto no site tem estoque preenchido; saldo 0 (ou sem
linha de EstoqueLoja na loja do site) = ESGOTADO — aparece na vitrine com
selo "Esgotado" e sem botão de comprar, mas NÃO some. Cestas incluídas.
A emissão de NF e o catálogo interno (`produtos_publicados`) veem TUDO,
porque a NF sai depois do pagamento, com o estoque já podendo estar zerado.
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


def _site_loja(db):
    from app.models import AppConfig, Loja
    loja = Loja(nome='Loja do Site', ativa=True, endereco='Rua Site, 1')
    db.session.add(loja)
    db.session.commit()
    AppConfig.set('loja_site_estoque_id', loja.id)
    db.session.commit()
    return loja


def _produto(db, nome, preco=20.0, categoria='Conservas'):
    from app.models import Produto
    p = Produto(nome=nome, categoria=categoria, preco_site=preco,
                imagem_dropbox_url='https://x/p.jpg', ativo=True)
    db.session.add(p)
    db.session.commit()
    return p


def _estoque(db, loja, produto, qtd):
    from app.models import EstoqueLoja
    el = EstoqueLoja(loja_id=loja.id, produto_id=produto.id, quantidade=qtd)
    db.session.add(el)
    db.session.commit()
    return el


def test_anotar_esgotado_marca_zero_e_sem_linha(app):
    from app.extensions import db
    from app.services import loja_catalogo
    with app.app_context():
        loja = _site_loja(db)
        com = _produto(db, 'Geleia com saldo')
        zero = _produto(db, 'Geleia zerada')
        _produto(db, 'Geleia sem linha')  # sem EstoqueLoja
        _estoque(db, loja, com, 5)
        _estoque(db, loja, zero, 0)
        itens = loja_catalogo.anotar_esgotado(
            loja_catalogo.produtos_publicados())
        por_nome = {i['nome']: i for i in itens}
        # NADA some — os 3 continuam na lista
        assert {'Geleia com saldo', 'Geleia zerada',
                'Geleia sem linha'} <= set(por_nome)
        assert por_nome['Geleia com saldo']['esgotado'] is False
        assert por_nome['Geleia zerada']['esgotado'] is True   # saldo 0
        assert por_nome['Geleia sem linha']['esgotado'] is True  # sem linha


def test_anotar_esgotado_fail_open_sem_loja_site(app):
    """Sem loja do site configurada → ninguém esgotado (fail-open)."""
    from app.extensions import db
    from app.services import loja_catalogo
    with app.app_context():
        _produto(db, 'Pao sem loja site')
        itens = loja_catalogo.anotar_esgotado(
            loja_catalogo.produtos_publicados())
        assert all(i['esgotado'] is False for i in itens)


def test_tem_estoque_site(app):
    from app.extensions import db
    from app.services import loja_catalogo
    with app.app_context():
        loja = _site_loja(db)
        com = _produto(db, 'Com saldo')
        zero = _produto(db, 'Zerado')
        _estoque(db, loja, com, 3)
        _estoque(db, loja, zero, 0)
        assert loja_catalogo.tem_estoque_site('produto', com.id) is True
        assert loja_catalogo.tem_estoque_site('produto', zero.id) is False
        assert loja_catalogo.tem_estoque_site('produto', 999999) is False


def test_pagina_produto_mostra_esgotado_sem_404(app):
    from app.extensions import db
    from app.services import loja_catalogo
    with app.app_context():
        loja = _site_loja(db)
        com = _produto(db, 'Cesta com saldo')
        zero = _produto(db, 'Cesta zerada')
        _estoque(db, loja, com, 4)
        _estoque(db, loja, zero, 0)
        href_com = next(i['href'] for i in loja_catalogo.produtos_publicados()
                        if i['nome'] == 'Cesta com saldo')
        href_zero = next(i['href'] for i in loja_catalogo.produtos_publicados()
                         if i['nome'] == 'Cesta zerada')
    c = _owner(app)  # logado → passa o gate
    r_com = c.get(href_com)
    assert r_com.status_code == 200
    assert b'Adicionar ao carrinho' in r_com.data
    r_zero = c.get(href_zero)
    assert r_zero.status_code == 200          # NÃO some (não é 404)
    assert b'selo-esgotado' in r_zero.data    # tem o selo
    assert b'Adicionar ao carrinho' not in r_zero.data  # sem botão de comprar


def test_home_mostra_item_esgotado_com_selo(app):
    from app.extensions import db
    with app.app_context():
        loja = _site_loja(db)
        zero = _produto(db, 'Box zerado na home')
        _estoque(db, loja, zero, 0)
    c = _owner(app)
    r = c.get('/loja/')
    assert r.status_code == 200
    assert 'Box zerado na home'.encode() in r.data  # aparece
    assert b'selo-esgotado' in r.data               # com selo


def test_checkout_remove_item_esgotado(app):
    from app.extensions import db
    from app.services import loja_checkout
    with app.app_context():
        loja = _site_loja(db)
        com = _produto(db, 'Box com saldo')
        zero = _produto(db, 'Box esgotado')
        _estoque(db, loja, com, 2)
        _estoque(db, loja, zero, 0)
        itens, avisos = loja_checkout.montar_itens([
            {'kind': 'produto', 'id': com.id, 'qtd': 1},
            {'kind': 'produto', 'id': zero.id, 'qtd': 1},
        ])
        nomes = [i['nome'] for i in itens]
        assert 'Box com saldo' in nomes
        assert 'Box esgotado' not in nomes      # esgotado não entra no pedido
        assert any('esgotou' in a for a in avisos)


def test_diagnostico_estoque_vitrine(app):
    from app.extensions import db
    with app.app_context():
        loja = _site_loja(db)
        com = _produto(db, 'Visivel')
        _produto(db, 'Esgotado sem linha')
        _estoque(db, loja, com, 7)
    c = _owner(app)
    r = c.get('/admin/loja-online/estoque-vitrine')
    assert r.status_code == 200
    j = r.get_json()
    assert j['loja_site'] == 'Loja do Site'
    assert j['em_estoque'] == 1
    assert j['esgotados'] == 1
    nomes_esgotados = [i['nome'] for i in j['itens_esgotados']]
    assert 'Esgotado sem linha' in nomes_esgotados

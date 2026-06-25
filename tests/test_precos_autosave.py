"""Auto-save por campo na tela de precos (POST /receitas/precos/salvar-campo).

Salva um unico campo de preco via AJAX quando o usuario sai do input —
sem depender do botao "Salvar todos". Owner-only (dinheiro).
"""
import json


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _post(client, payload):
    return client.post('/receitas/precos/salvar-campo',
                       data=json.dumps(payload),
                       content_type='application/json')


def test_salva_campo_de_receita(app, owner_user):
    from app.extensions import db
    from app.models import Receita
    with app.app_context():
        r = Receita(nome='Pao Autosave', categoria='Paes', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=50.0)
        db.session.add(r)
        db.session.commit()
        rid = r.id

    client = app.test_client()
    _login(client, owner_user)
    resp = _post(client, {'tipo': 'receita', 'id': rid,
                          'campo': 'preco_venda', 'valor': '12,50'})
    assert resp.status_code == 200
    assert resp.get_json()['ok'] is True
    with app.app_context():
        assert Receita.query.get(rid).preco_venda == 12.50


def test_salva_campo_de_produto_atacado(app, owner_user):
    from app.extensions import db
    from app.models import Produto
    with app.app_context():
        p = Produto(nome='Cesta Autosave', categoria='Cestas', ativo=True)
        db.session.add(p)
        db.session.commit()
        pid = p.id

    client = app.test_client()
    _login(client, owner_user)
    resp = _post(client, {'tipo': 'produto', 'id': pid,
                          'campo': 'preco_atacado', 'valor': '99,00'})
    assert resp.status_code == 200
    with app.app_context():
        assert Produto.query.get(pid).preco_atacado == 99.00


def test_valor_vazio_zera_para_null(app, owner_user):
    from app.extensions import db
    from app.models import Receita
    with app.app_context():
        r = Receita(nome='Limpa Autosave', categoria='X', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=10.0, preco_loja=5.0)
        db.session.add(r)
        db.session.commit()
        rid = r.id

    client = app.test_client()
    _login(client, owner_user)
    resp = _post(client, {'tipo': 'receita', 'id': rid,
                          'campo': 'preco_loja', 'valor': ''})
    assert resp.status_code == 200
    with app.app_context():
        assert Receita.query.get(rid).preco_loja is None


def test_campo_invalido_para_tipo_rejeitado(app, owner_user):
    """preco_atacado nao existe em Receita (la eh preco_venda). 400."""
    from app.extensions import db
    from app.models import Receita
    with app.app_context():
        r = Receita(nome='Cross Autosave', categoria='X', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=10.0)
        db.session.add(r)
        db.session.commit()
        rid = r.id

    client = app.test_client()
    _login(client, owner_user)
    resp = _post(client, {'tipo': 'receita', 'id': rid,
                          'campo': 'preco_atacado', 'valor': '10'})
    assert resp.status_code == 400


def test_valor_fora_da_faixa_rejeitado(app, owner_user):
    from app.extensions import db
    from app.models import Receita
    with app.app_context():
        r = Receita(nome='Faixa Autosave', categoria='X', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=10.0, preco_loja=3.0)
        db.session.add(r)
        db.session.commit()
        rid = r.id

    client = app.test_client()
    _login(client, owner_user)
    resp = _post(client, {'tipo': 'receita', 'id': rid,
                          'campo': 'preco_loja', 'valor': '999999'})
    assert resp.status_code == 400
    with app.app_context():
        assert Receita.query.get(rid).preco_loja == 3.0  # nao mudou


def test_autosave_exige_owner(app, admin_user):
    """admin comum (sem is_owner) nao pode auto-salvar preco."""
    from app.extensions import db
    from app.models import Receita
    with app.app_context():
        r = Receita(nome='Gate Autosave', categoria='X', rendimento_qtd=1,
                    rendimento_unidade='un', peso_base=10.0)
        db.session.add(r)
        db.session.commit()
        rid = r.id

    client = app.test_client()
    _login(client, admin_user)
    resp = _post(client, {'tipo': 'receita', 'id': rid,
                          'campo': 'preco_loja', 'valor': '5'})
    assert resp.status_code == 403

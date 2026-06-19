"""Ajuste de estoque de loja aceita PRODUTO (cesta/conserva/bebida).

Bug histórico (corrigido 19/06/2026): `estoque_loja_ajuste` só ramificava
receita vs else→matéria-prima, então um produto (`p_<id>`) era gravado como
`materia_prima_id` com o id errado — corrupção de estoque. Agora trata
produto direto, e o modal lista produtos.
"""


def _admin(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Adm', login='adm', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def test_ajuste_entrada_cria_linha_de_produto(app):
    from app.extensions import db
    from app.models import EstoqueLoja, Loja, Produto
    with app.app_context():
        loja = Loja(nome='Loja Estoque', ativa=True, endereco='Rua A, 1')
        p = Produto(nome='Box Mimo', categoria='Cestas', preco_site=80,
                    ativo=True)
        db.session.add_all([loja, p])
        db.session.commit()
        loja_id, pid = loja.id, p.id
    c = _admin(app)
    r = c.post('/pedidos/estoque-loja/ajuste', data={
        'loja_id': loja_id, 'item_id': f'p_{pid}',
        'quantidade': 10, 'operacao': 'entrada',
        'motivo': 'estoque inicial do site',
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        el = EstoqueLoja.query.filter_by(loja_id=loja_id, produto_id=pid).first()
        assert el is not None and el.quantidade == 10
        # NÃO virou matéria-prima (o bug antigo)
        assert el.materia_prima_id is None
        # e não existe linha de MP com o id do produto
        assert EstoqueLoja.query.filter_by(
            loja_id=loja_id, materia_prima_id=pid).first() is None


def test_modal_ajuste_lista_produtos(app):
    """A tela de estoque-loja (admin) lista produtos no dropdown de ajuste."""
    from app.extensions import db
    from app.models import Loja, Produto
    with app.app_context():
        loja = Loja(nome='Loja Drop', ativa=True, endereco='Rua B, 2')
        db.session.add(loja)
        db.session.add(Produto(nome='Geleia de Morango', categoria='Conservas',
                               preco_site=18, ativo=True))
        db.session.commit()
        loja_id = loja.id
    c = _admin(app)
    r = c.get(f'/pedidos/estoque-loja?loja={loja_id}')
    assert r.status_code == 200
    assert b'Geleia de Morango' in r.data  # produto aparece no select de ajuste

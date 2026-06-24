"""Transferência de vínculos entre receitas (fusão de duplicata, ex.
"Molho Pesto 100g" -> "Molho Pesto"): histórico não se apaga, se REAPONTA.
Estoque funde com a linha equivalente do destino somando quantidades e
reapontando movimentações — invariante: nada se perde."""


def _login(c):
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def _receita(db, nome):
    from app.models import Receita
    r = Receita(nome=nome, categoria='cremes', rendimento_qtd=10,
                rendimento_unidade='un', peso_base=1000.0)
    db.session.add(r)
    db.session.flush()
    return r


def test_transferencia_completa_e_exclusao(app, admin_user):
    from app.extensions import db
    from app.models import (
        EstoqueLoja,
        EstoqueProducao,
        Loja,
        MovEstoqueLoja,
        PedidoItem,
        PedidoLoja,
        PrecoLojaReceita,
        Receita,
        SeruProdutoMap,
    )
    with app.app_context():
        origem = _receita(db, 'Molho Pesto 100g')
        destino = _receita(db, 'Molho Pesto')
        loja = Loja(nome='Centro', ativa=True)
        db.session.add(loja)
        db.session.flush()
        oid, did, lid = origem.id, destino.id, loja.id

        # pedido historico
        p = PedidoLoja(loja_id=lid, status='entregue')
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=oid, quantidade=5))

        # estoque LOJA: destino JA tem linha equivalente -> funde (7+3=10)
        el_dest = EstoqueLoja(loja_id=lid, receita_id=did, quantidade=7, estado=None)
        el_orig = EstoqueLoja(loja_id=lid, receita_id=oid, quantidade=3, estado=None)
        db.session.add_all([el_dest, el_orig])
        db.session.flush()
        db.session.add(MovEstoqueLoja(estoque_loja_id=el_orig.id, tipo='entrada',
                                      quantidade=3, referencia='lote antigo'))
        el_dest_id, el_orig_id = el_dest.id, el_orig.id

        # estoque PRODUCAO: destino NAO tem equivalente -> so reaponta
        db.session.add(EstoqueProducao(receita_id=oid, quantidade=4, estado=None))

        # mapeamento PDV confirmado + preco por loja conflitante
        db.session.add(SeruProdutoMap(seru_nome='PESTO 100G', receita_id=oid))
        db.session.add_all([
            PrecoLojaReceita(loja_id=lid, receita_id=oid, preco=9.0),
            PrecoLojaReceita(loja_id=lid, receita_id=did, preco=11.0),
        ])
        db.session.commit()

    c = app.test_client()
    _login(c)

    # destino errado nao passa
    assert c.post(f'/receitas/{oid}/vinculos/transferir',
                  data={'destino': 'Nao Existe'}).status_code == 400
    assert c.post(f'/receitas/{oid}/vinculos/transferir',
                  data={'destino': 'Molho Pesto 100g'}).status_code == 400

    r = c.post(f'/receitas/{oid}/vinculos/transferir',
               data={'destino': 'molho pesto'})   # case-insensitive
    data = r.get_json()
    assert r.status_code == 200
    assert data['pode_excluir'] is True
    assert data['destino'] == 'Molho Pesto'
    assert data['movidos']['pedidos'] == 1
    assert data['movidos']['estoque_loja'] == 1

    with app.app_context():
        # pedido reapontado, historico intacto
        pi = PedidoItem.query.first()
        assert pi.receita_id == did and pi.quantidade == 5
        # estoque loja fundido: 7+3, linha da origem sumiu, mov sobreviveu
        # na linha que ficou
        assert db.session.get(EstoqueLoja, el_orig_id) is None
        el = db.session.get(EstoqueLoja, el_dest_id)
        assert el.quantidade == 10
        mov = MovEstoqueLoja.query.filter_by(referencia='lote antigo').one()
        assert mov.estoque_loja_id == el_dest_id
        # estoque producao reapontado (sem equivalente no destino)
        ep = EstoqueProducao.query.one()
        assert ep.receita_id == did and ep.quantidade == 4
        # mapeamento seguiu
        assert SeruProdutoMap.query.one().receita_id == did
        # preco conflitante: prevaleceu o do destino
        precos = PrecoLojaReceita.query.filter_by(loja_id=lid).all()
        assert len(precos) == 1
        assert precos[0].receita_id == did and precos[0].preco == 11.0

    # e agora a duplicata sai sem resistencia
    c.post(f'/receitas/{oid}/excluir', follow_redirects=True)
    with app.app_context():
        assert db.session.get(Receita, oid) is None
        assert db.session.get(Receita, did) is not None

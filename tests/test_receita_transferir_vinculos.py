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
        VendaMapa,
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
        db.session.add(VendaMapa(canal='seru', nome_externo='PESTO 100G', receita_id=oid))
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
        assert VendaMapa.query.one().receita_id == did
        # preco conflitante: prevaleceu o do destino
        precos = PrecoLojaReceita.query.filter_by(loja_id=lid).all()
        assert len(precos) == 1
        assert precos[0].receita_id == did and precos[0].preco == 11.0

    # e agora a duplicata sai sem resistencia
    c.post(f'/receitas/{oid}/excluir', follow_redirects=True)
    with app.app_context():
        assert db.session.get(Receita, oid) is None
        assert db.session.get(Receita, did) is not None


def test_transferencia_para_materia_prima(app, admin_user):
    """Receita que na verdade é insumo COMPRADO (ex: "pão de queijo (saco)"):
    transfere pra uma MP tudo que tem coluna de MP (pedidos, desperdício,
    estoque de loja com fusão, cestas, mapeamentos, ingrediente em fichas);
    o que não tem (planos de produção) FICA e é reportado em `ficaram`."""
    from app.extensions import db
    from app.models import (
        Desperdicio,
        EstoqueLoja,
        Loja,
        MateriaPrima,
        MovEstoqueLoja,
        PedidoItem,
        PedidoLoja,
        PlanejamentoItem,
        PlanejamentoProducao,
        Produto,
        ProdutoItem,
        ReceitaIngrediente,
        VendaMapa,
    )
    from app.utils import hoje
    with app.app_context():
        origem = _receita(db, 'pão de queijo (saco)')
        mp = MateriaPrima(nome='Pão de Queijo Congelado (saco)', unidade='un',
                          custo_por_kg=30.0)
        loja = Loja(nome='Centro', ativa=True)
        db.session.add_all([mp, loja])
        db.session.flush()
        oid, mpid, lid = origem.id, mp.id, loja.id

        # pedido histórico + desperdício
        p = PedidoLoja(loja_id=lid, status='entregue')
        db.session.add(p)
        db.session.flush()
        db.session.add(PedidoItem(pedido_id=p.id, receita_id=oid, quantidade=5))
        db.session.add(Desperdicio(loja_id=lid, receita_id=oid, quantidade=1))

        # estoque loja: a MP JÁ tem linha na mesma loja -> funde (4+2=6)
        el_mp = EstoqueLoja(loja_id=lid, materia_prima_id=mpid, quantidade=4)
        el_orig = EstoqueLoja(loja_id=lid, receita_id=oid, quantidade=2)
        db.session.add_all([el_mp, el_orig])
        db.session.flush()
        db.session.add(MovEstoqueLoja(estoque_loja_id=el_orig.id, tipo='entrada',
                                      quantidade=2, referencia='entrega antiga'))
        el_mp_id, el_orig_id = el_mp.id, el_orig.id

        # cesta que usa a receita + mapeamento Seru + ingrediente em outra ficha
        cesta = Produto(nome='Kit Lanche', ativo=True)
        db.session.add(cesta)
        db.session.flush()
        db.session.add(ProdutoItem(produto_id=cesta.id, tipo='receita',
                                   receita_id=oid, item_nome=origem.nome,
                                   quantidade=1))
        db.session.add(VendaMapa(canal='seru', nome_externo='PAO DE QUEIJO',
                                 receita_id=oid))
        outra = _receita(db, 'Combo da Tarde')
        db.session.add(ReceitaIngrediente(receita_id=outra.id, tipo='receita',
                                          ingrediente_nome=origem.nome,
                                          sub_receita_id=oid, porcentagem=10))

        # plano de produção: NÃO tem coluna de MP -> fica
        plano = PlanejamentoProducao(data=hoje(), origem='cronograma',
                                     status='aprovado', enviado_ao_padeiro=True)
        db.session.add(plano)
        db.session.flush()
        db.session.add(PlanejamentoItem(planejamento_id=plano.id,
                                        receita_id=oid, multiplicador=1,
                                        qtd_alvo=10))
        db.session.commit()
        outra_id, cesta_id = outra.id, cesta.id

    c = app.test_client()
    _login(c)

    # MP inexistente e tipo inválido não passam
    assert c.post(f'/receitas/{oid}/vinculos/transferir',
                  data={'destino': 'Nao Existe', 'tipo_destino': 'mp'}
                  ).status_code == 400
    assert c.post(f'/receitas/{oid}/vinculos/transferir',
                  data={'destino': 'x', 'tipo_destino': 'banana'}
                  ).status_code == 400

    r = c.post(f'/receitas/{oid}/vinculos/transferir',
               data={'destino': 'pão de queijo congelado (saco)',  # case-insens.
                     'tipo_destino': 'mp'})
    data = r.get_json()
    assert r.status_code == 200
    assert data['tipo_destino'] == 'mp'
    assert data['movidos']['pedidos'] == 1
    assert data['movidos']['desperdicio'] == 1
    assert data['movidos']['estoque_loja'] == 1
    assert data['movidos']['cestas'] == 1
    assert data['movidos']['mapeamentos'] == 1
    assert data['movidos']['ingrediente_em_fichas'] == 1
    assert data['ficaram'] == {'planejamento': 1}
    assert data['pode_excluir'] is False           # plano segura a exclusão

    with app.app_context():
        pi = PedidoItem.query.one()
        assert pi.receita_id is None and pi.materia_prima_id == mpid
        assert pi.quantidade == 5                   # histórico intacto
        d = Desperdicio.query.one()
        assert d.receita_id is None and d.materia_prima_id == mpid
        # estoque fundido na linha da MP; mov reapontado; linha origem sumiu
        assert db.session.get(EstoqueLoja, el_orig_id) is None
        el = db.session.get(EstoqueLoja, el_mp_id)
        assert el.quantidade == 6
        mov = MovEstoqueLoja.query.filter_by(referencia='entrega antiga').one()
        assert mov.estoque_loja_id == el_mp_id
        # cesta virou componente de MP com nome corrigido
        pit = ProdutoItem.query.filter_by(produto_id=cesta_id).one()
        assert pit.tipo == 'mp' and pit.materia_prima_id == mpid
        assert pit.receita_id is None
        assert pit.item_nome == 'Pão de Queijo Congelado (saco)'
        # mapeamento manteve o nome externo e virou MP
        vm = VendaMapa.query.one()
        assert vm.materia_prima_id == mpid and vm.receita_id is None
        assert vm.estado == 'mapeado'
        # ingrediente da outra ficha virou MP (por nome, FK limpo)
        ing = ReceitaIngrediente.query.filter_by(receita_id=outra_id).one()
        assert ing.tipo == 'mp'
        assert ing.ingrediente_nome == 'Pão de Queijo Congelado (saco)'
        assert ing.sub_receita_id is None
        # plano de produção FICOU na receita (histórico)
        assert PlanejamentoItem.query.one().receita_id == oid


def test_transferencia_receita_corrige_fk_de_ingrediente(app, admin_user):
    """REGRESSÃO: a transferência receita→receita atualizava só o NOME do
    ingrediente nas outras fichas; o FK sub_receita_id (que o BOM usa e que
    bloqueia a exclusão) ficava preso na origem."""
    from app.extensions import db
    from app.models import Receita, ReceitaIngrediente
    with app.app_context():
        origem = _receita(db, 'Massa Velha')
        destino = _receita(db, 'Massa Nova')
        outra = _receita(db, 'Croissant X')
        db.session.add(ReceitaIngrediente(receita_id=outra.id, tipo='receita',
                                          ingrediente_nome=origem.nome,
                                          sub_receita_id=origem.id,
                                          porcentagem=50))
        db.session.commit()
        oid, did, outra_id = origem.id, destino.id, outra.id

    c = app.test_client()
    _login(c)
    r = c.post(f'/receitas/{oid}/vinculos/transferir',
               data={'destino': 'Massa Nova'})
    assert r.status_code == 200
    assert r.get_json()['pode_excluir'] is True

    with app.app_context():
        ing = ReceitaIngrediente.query.filter_by(receita_id=outra_id).one()
        assert ing.sub_receita_id == did            # FK seguiu junto
        assert ing.ingrediente_nome == 'Massa Nova'
        # agora a origem sai de verdade
        c.post(f'/receitas/{oid}/excluir', follow_redirects=True)
        assert db.session.get(Receita, oid) is None

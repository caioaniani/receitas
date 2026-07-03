"""Exclusão de desperdício com estorno EXATO (03/07/2026).

A primeira versão da rota estornava `desp.quantidade` às cegas — criava
estoque fantasma em três casos: reaproveitável (nunca baixou), baixa parcial
(saiu menos que o registrado) e cesta (a baixa foi nos componentes, não no
produto). Agora cada MovEstoqueLoja de desperdício carrega `desperdicio_id`
e a exclusão devolve exatamente o que os movimentos vinculados baixaram;
registro antigo (sem vínculo) é excluído SEM mexer em estoque, com aviso.
"""
from app.extensions import db
from app.models import Desperdicio, EstoqueLoja, Loja, MovEstoqueLoja, Receita


def _setup(reaproveitavel=False, qtd_estoque=10, nome='Item Estorno'):
    loja = Loja(nome='Loja Estorno', ativa=True)
    r = Receita(nome=nome, categoria='Paes', rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0,
                reaproveitavel=reaproveitavel)
    db.session.add_all([loja, r])
    db.session.commit()
    el = EstoqueLoja(loja_id=loja.id, receita_id=r.id, quantidade=qtd_estoque)
    db.session.add(el)
    db.session.commit()
    return loja, r, el


def _login(client, uid):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True


def _registrar_web(app, admin_user, loja, receita, qtd, motivo='estragou'):
    c = app.test_client()
    _login(c, admin_user.id)
    resp = c.post('/pedidos/desperdicio', data={
        'loja_id': str(loja.id), 'item_id': f'r_{receita.id}',
        'quantidade': str(qtd), 'motivo': motivo,
    })
    assert resp.status_code in (302, 303)
    return c


def _excluir(client, desp_id, loja_id):
    return client.post(f'/pedidos/desperdicio/{desp_id}/excluir',
                       follow_redirects=True)


def test_registro_web_vincula_movimento(app, admin_user):
    with app.app_context():
        loja, r, el = _setup()
        _registrar_web(app, admin_user, loja, r, qtd=4)
        desp = Desperdicio.query.filter_by(receita_id=r.id).first()
        mov = MovEstoqueLoja.query.filter_by(estoque_loja_id=el.id,
                                             tipo='desperdicio').first()
        assert mov is not None
        assert mov.desperdicio_id == desp.id


def test_excluir_estorna_exatamente_o_baixado(app, admin_user):
    with app.app_context():
        loja, r, el = _setup(qtd_estoque=10)
        c = _registrar_web(app, admin_user, loja, r, qtd=4)
        db.session.refresh(el)
        assert el.quantidade == 6
        desp = Desperdicio.query.filter_by(receita_id=r.id).first()
        resp = _excluir(c, desp.id, loja.id)
        assert '4 un devolvida(s)' in resp.get_data(as_text=True)
        db.session.refresh(el)
        assert el.quantidade == 10
        assert Desperdicio.query.filter_by(receita_id=r.id).count() == 0
        estorno = MovEstoqueLoja.query.filter_by(
            estoque_loja_id=el.id, tipo='desperdicio_estorno').first()
        assert estorno is not None and estorno.quantidade == 4


def test_excluir_baixa_parcial_devolve_so_o_que_saiu(app, admin_user):
    """Registrou 10 com saldo 3: baixou 3 (+7 sem_estoque). O estorno
    devolve 3 — devolver 10 criaria 7 un fantasma."""
    with app.app_context():
        loja, r, el = _setup(qtd_estoque=3)
        c = _registrar_web(app, admin_user, loja, r, qtd=10)
        db.session.refresh(el)
        assert el.quantidade == 0
        desp = Desperdicio.query.filter_by(receita_id=r.id).first()
        _excluir(c, desp.id, loja.id)
        db.session.refresh(el)
        assert el.quantidade == 3


def test_excluir_reaproveitavel_nao_mexe_no_estoque(app, admin_user):
    """Croissant reaproveitável + validade não baixou nada no registro —
    a exclusão NÃO pode creditar (era o pior caso do estorno cego: +15
    fantasma no caso real da Nebraska)."""
    with app.app_context():
        loja, r, el = _setup(reaproveitavel=True, qtd_estoque=10)
        c = _registrar_web(app, admin_user, loja, r, qtd=15, motivo='validade')
        db.session.refresh(el)
        assert el.quantidade == 10                    # registro sem baixa
        desp = Desperdicio.query.filter_by(receita_id=r.id).first()
        resp = _excluir(c, desp.id, loja.id)
        assert 'nada a estornar' in resp.get_data(as_text=True)
        db.session.refresh(el)
        assert el.quantidade == 10                    # continua intacto
        assert MovEstoqueLoja.query.filter_by(
            estoque_loja_id=el.id).count() == 0


def test_excluir_registro_antigo_sem_vinculo_avisa_e_nao_mexe(app, admin_user):
    """Registro anterior à coluna (sem movimento vinculado): exclui, avisa
    e NÃO chuta estoque."""
    with app.app_context():
        loja, r, el = _setup(qtd_estoque=10)
        desp = Desperdicio(loja_id=loja.id, receita_id=r.id, quantidade=5,
                           motivo='validade', criado_por_id=admin_user.id)
        db.session.add(desp)
        db.session.commit()
        c = app.test_client()
        _login(c, admin_user.id)
        resp = _excluir(c, desp.id, loja.id)
        html = resp.get_data(as_text=True)
        assert 'Registro antigo' in html
        db.session.refresh(el)
        assert el.quantidade == 10
        assert MovEstoqueLoja.query.filter_by(
            estoque_loja_id=el.id).count() == 0


def test_excluir_cesta_devolve_componentes(app, admin_user):
    """A baixa de cesta acontece nos COMPONENTES — o estorno devolve neles
    (o estorno cego creditava o produto-cesta, que a loja nem estoca)."""
    from app.models import Produto, ProdutoItem
    with app.app_context():
        loja, r, el = _setup(qtd_estoque=20, nome='Pao Comp Estorno')
        cesta = Produto(nome='Box Estorno', ativo=True)
        db.session.add(cesta)
        db.session.flush()
        db.session.add(ProdutoItem(produto_id=cesta.id, tipo='receita',
                                   item_nome=r.nome, receita_id=r.id,
                                   quantidade=5))
        db.session.commit()
        c = app.test_client()
        _login(c, admin_user.id)
        resp = c.post('/pedidos/desperdicio', data={
            'loja_id': str(loja.id), 'item_id': f'p_{cesta.id}',
            'quantidade': '2', 'motivo': 'estragou',
        })
        assert resp.status_code in (302, 303)
        db.session.refresh(el)
        assert el.quantidade == 10                    # 20 - 2x5
        desp = Desperdicio.query.filter_by(produto_id=cesta.id).first()
        _excluir(c, desp.id, loja.id)
        db.session.refresh(el)
        assert el.quantidade == 20                    # componentes de volta
        # E nada foi creditado no produto-cesta.
        el_cesta = EstoqueLoja.query.filter_by(loja_id=loja.id,
                                               produto_id=cesta.id).first()
        assert el_cesta is None or (el_cesta.quantidade or 0) == 0


def test_lote_copilot_vincula_movimentos(app, admin_user):
    from app.services.copilot import executar_registrar_desperdicio_lote
    with app.app_context():
        loja, r, el = _setup(qtd_estoque=10, nome='Pao Lote FK')
        res = executar_registrar_desperdicio_lote({
            'loja_id': loja.id, 'motivo': 'estragou',
            'itens': [{'nome': 'Pao Lote FK', 'quantidade': 12}],
        }, admin_user)
        assert res['ok'] is True
        desp = Desperdicio.query.filter_by(receita_id=r.id).first()
        movs = MovEstoqueLoja.query.filter_by(desperdicio_id=desp.id).all()
        # baixa de 10 + sem_estoque de 2, ambos vinculados
        assert {m.tipo for m in movs} == {'desperdicio',
                                          'desperdicio_sem_estoque'}
        assert all(m.desperdicio_id == desp.id for m in movs)

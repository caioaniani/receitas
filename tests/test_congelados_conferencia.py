"""Conferencia fisica do estoque da industria (EstoqueProducao):
sistema x fisico, ajuste com auditoria, e adicao de item que faltou."""


def _login(c):
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def _receita(db, nome, categoria='Paes'):
    from app.models import Receita
    r = Receita(nome=nome, categoria=categoria, rendimento_qtd=1,
                rendimento_unidade='un', peso_base=100.0)
    db.session.add(r)
    db.session.flush()
    return r


def test_conferencia_renderiza_e_ajusta_existente(app, admin_user):
    from app.extensions import db
    from app.models import EstoqueProducao, MovEstoqueProducao
    with app.app_context():
        r = _receita(db, 'Pao Frances')
        ep = EstoqueProducao(receita_id=r.id, quantidade=10)
        db.session.add(ep)
        db.session.commit()
        ep_id = ep.id

    c = app.test_client()
    _login(c)
    rget = c.get('/pedidos/congelados/conferencia')
    assert rget.status_code == 200
    assert b'Pao Frances' in rget.data

    # contagem fisica = 7 (sistema 10 -> diff -3)
    rpost = c.post('/pedidos/congelados/conferencia',
                   data={f'real_{ep_id}': '7'}, follow_redirects=True)
    assert rpost.status_code == 200
    with app.app_context():
        ep = db.session.get(EstoqueProducao, ep_id)
        assert ep.quantidade == 7
        mov = MovEstoqueProducao.query.filter_by(
            estoque_producao_id=ep_id, tipo='ajuste_conferencia').first()
        assert mov is not None and mov.quantidade == -3   # saida registrada com sinal


def test_conferencia_diff_zero_nao_cria_mov(app, admin_user):
    from app.extensions import db
    from app.models import EstoqueProducao, MovEstoqueProducao
    with app.app_context():
        r = _receita(db, 'Sonho', categoria='Doces')
        ep = EstoqueProducao(receita_id=r.id, quantidade=5)
        db.session.add(ep)
        db.session.commit()
        ep_id = ep.id

    c = app.test_client()
    _login(c)
    c.post('/pedidos/congelados/conferencia',
           data={f'real_{ep_id}': '5'}, follow_redirects=True)
    with app.app_context():
        assert db.session.get(EstoqueProducao, ep_id).quantidade == 5
        assert MovEstoqueProducao.query.filter_by(estoque_producao_id=ep_id).count() == 0


def test_conferencia_adiciona_item_novo(app, admin_user):
    """Item contado no fisico que ainda nao tem linha: cria EstoqueProducao."""
    from app.extensions import db
    from app.models import EstoqueProducao
    with app.app_context():
        r = _receita(db, 'Croissant', categoria='Folhados')
        db.session.commit()
        r_id = r.id

    c = app.test_client()
    _login(c)
    c.post('/pedidos/congelados/conferencia',
           data={'novo_alvo': f'receita:{r_id}', 'novo_qtd': '12'},
           follow_redirects=True)
    with app.app_context():
        ep = EstoqueProducao.query.filter_by(receita_id=r_id, estado=None).first()
        assert ep is not None and ep.quantidade == 12

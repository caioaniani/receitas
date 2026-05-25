"""Telas de canal->loja, item->MP e variacoes: render + acoes."""


def _login(c):
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def test_tela_canais_e_vincular(app, admin_user):
    from app.extensions import db
    from app.models import Loja, SlackCanalLojaMap
    app.config['SLACK_CANAIS_NF'] = 'C_IND,C_LOJA'
    app.config['SLACK_CANAIS_NF_NOMES'] = 'C_IND=Industria;C_LOJA=Ribeiro do Vale'
    with app.app_context():
        db.session.add_all([Loja(nome='Industria', ativa=True),
                            Loja(nome='Ribeiro do Vale', ativa=True)])
        db.session.commit()
        rib_id = Loja.query.filter_by(nome='Ribeiro do Vale').first().id

    c = app.test_client()
    _login(c)
    r = c.get('/contas-pagar/canais')
    assert r.status_code == 200
    assert 'Ribeiro do Vale'.encode() in r.data

    # auto-fuzzy: o canal da industria deve ter sido detectado como eh_industria
    with app.app_context():
        ind = SlackCanalLojaMap.query.filter_by(canal_id='C_IND').first()
        assert ind is not None and ind.eh_industria is True

    r2 = c.post('/contas-pagar/canais/C_LOJA',
                data={'acao': 'vincular', 'loja_id': str(rib_id)},
                follow_redirects=True)
    assert r2.status_code == 200
    with app.app_context():
        m = SlackCanalLojaMap.query.filter_by(canal_id='C_LOJA').first()
        assert m.loja_id == rib_id and m.confirmado_em is not None


def test_tela_mapeamentos_e_vincular(app, admin_user):
    from app.extensions import db
    from app.models import ContaPagarItemMap, MateriaPrima
    from app.services import conta_pagar_estoque as svc
    with app.app_context():
        mp = MateriaPrima(nome='Acucar', unidade='kg', custo_por_kg=0)
        db.session.add(mp)
        db.session.flush()
        mp_id = mp.id
        db.session.add(ContaPagarItemMap(
            item_nome_norm=svc.normalizar_item_nome('ACUCAR CRISTAL'),
            item_nome_exemplo='ACUCAR CRISTAL'))
        db.session.commit()

    c = app.test_client()
    _login(c)
    r = c.get('/contas-pagar/mapeamentos')
    assert r.status_code == 200
    assert b'ACUCAR CRISTAL' in r.data

    with app.app_context():
        mid = ContaPagarItemMap.query.first().id
    r2 = c.post(f'/contas-pagar/mapeamentos/{mid}',
                data={'acao': 'vincular', 'materia_prima_id': str(mp_id),
                      'unidade_compra': 'sc', 'fator_conversao': '25',
                      'estado': 'pendente'},
                follow_redirects=True)
    assert r2.status_code == 200
    with app.app_context():
        m = db.session.get(ContaPagarItemMap, mid)
        assert m.materia_prima_id == mp_id
        assert m.fator_conversao == 25
        assert m.confirmado_em is not None


def test_tela_variacoes_e_aprovar(app, admin_user):
    from app.extensions import db
    from app.models import MateriaPrima, VariacaoPrecoMP
    with app.app_context():
        mp = MateriaPrima(nome='Manteiga', unidade='kg', custo_por_kg=10)
        db.session.add(mp)
        db.session.flush()
        db.session.add(VariacaoPrecoMP(
            materia_prima_id=mp.id, custo_anterior=8, custo_novo=10,
            variacao_pct=25, status='novo'))
        db.session.commit()

    c = app.test_client()
    _login(c)
    r = c.get('/contas-pagar/variacoes')
    assert r.status_code == 200
    assert b'Manteiga' in r.data

    with app.app_context():
        vid = VariacaoPrecoMP.query.first().id
    r2 = c.post(f'/contas-pagar/variacoes/{vid}',
                data={'acao': 'aprovar', 'f': 'todos'}, follow_redirects=True)
    assert r2.status_code == 200
    with app.app_context():
        v = db.session.get(VariacaoPrecoMP, vid)
        assert v.status == 'aprovado' and v.revisado_em is not None

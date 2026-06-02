"""Lista de compras semanal por loja — modelos, serviço e rotas."""
from datetime import date


def _login(client, uid):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
        sess['_fresh'] = True


def _setup_loja_com_itens(app, papel='gerente', is_owner=False):
    """Cria uma loja, um usuário com `papel` vinculado a ela, e alguns itens."""
    from app.extensions import db
    from app.models import ItemListaCompras, Loja, Usuario
    with app.app_context():
        loja = Loja(nome='Ribeiro do Vale', ativa=True)
        db.session.add(loja)
        db.session.flush()
        u = Usuario(login=f'u_{papel}', nome=papel.capitalize(),
                    papel=papel, loja_id=loja.id, is_owner=is_owner)
        u.set_senha('x')
        db.session.add(u)
        # 2 grupos, 2 itens cada
        for ordem, (g, n) in enumerate([
            ('AROMAR', 'CANELA EM PÓ'),
            ('AROMAR', 'TODDY'),
            ('CASTELÃO', 'PEITO DE PERU'),
            ('CASTELÃO', 'GUARANÁ'),
        ]):
            db.session.add(ItemListaCompras(loja_id=loja.id, grupo=g,
                                            nome_item=n, ordem=ordem))
        db.session.commit()
        return loja.id, u.id


def test_domingo_da_semana():
    from app.services.lista_compras_svc import domingo_da_semana
    # 2026-05-29 é sexta-feira (weekday=4) → domingo = 24
    assert domingo_da_semana(date(2026, 5, 29)) == date(2026, 5, 24)
    # domingo retorna ele mesmo
    assert domingo_da_semana(date(2026, 5, 24)) == date(2026, 5, 24)
    # segunda-feira (weekday=0) → domingo do dia anterior
    assert domingo_da_semana(date(2026, 5, 25)) == date(2026, 5, 24)
    # sabado (weekday=5) → domingo 6 dias antes
    assert domingo_da_semana(date(2026, 5, 30)) == date(2026, 5, 24)


def test_obter_ou_criar_semana_idempotente(app):
    from app.services import lista_compras_svc as svc
    loja_id, _ = _setup_loja_com_itens(app)
    with app.app_context():
        sem1 = svc.obter_ou_criar_semana(loja_id, data_inicio=date(2026, 5, 24))
        sem2 = svc.obter_ou_criar_semana(loja_id, data_inicio=date(2026, 5, 24))
        assert sem1.id == sem2.id
        assert sem1.status == 'aberta'


def test_salvar_tenho_e_envio(app):
    from app.extensions import db
    from app.models import ItemListaCompras
    from app.services import lista_compras_svc as svc
    loja_id, uid = _setup_loja_com_itens(app)
    with app.app_context():
        sem = svc.obter_ou_criar_semana(loja_id, data_inicio=date(2026, 5, 24))
        item = ItemListaCompras.query.filter_by(loja_id=loja_id).first()
        ok, _ = svc.salvar_tenho(sem, item.id, 5)
        assert ok
        # auto-save de novo (idempotente, atualiza)
        ok, _ = svc.salvar_tenho(sem, item.id, 8)
        assert ok
        db.session.refresh(sem)
        q = sem.quantidades[0]
        assert q.tenho == 8
        # negativo vira 0
        ok, _ = svc.salvar_tenho(sem, item.id, -3)
        assert ok and q.tenho == 0
        # invalido falha graciosamente
        ok, erro = svc.salvar_tenho(sem, item.id, 'abc')
        assert not ok and 'invalida' in (erro or '').lower()

        # enviar bloqueia novos saves
        ok, _ = svc.enviar_semana(sem, uid)
        assert ok and sem.status == 'enviada'
        ok, erro = svc.salvar_tenho(sem, item.id, 10)
        assert not ok and 'enviada' in (erro or '').lower()


def test_pedido_sobrou_e_fechar(app):
    from app.models import ItemListaCompras
    from app.services import lista_compras_svc as svc
    loja_id, uid = _setup_loja_com_itens(app)
    with app.app_context():
        sem = svc.obter_ou_criar_semana(loja_id, data_inicio=date(2026, 5, 24))
        item = ItemListaCompras.query.filter_by(loja_id=loja_id).first()
        svc.salvar_pedido_sobrou(sem, item.id, pedido=12, sobrou=3)
        assert sem.quantidades[0].pedido == 12
        assert sem.quantidades[0].sobrou == 3
        svc.fechar_semana(sem, uid)
        assert sem.status == 'fechada'
        # depois de fechada, bloqueia
        ok, erro = svc.salvar_pedido_sobrou(sem, item.id, pedido=5)
        assert not ok and 'fechada' in (erro or '').lower()


def test_historico_anterior(app):
    from app.models import ItemListaCompras
    from app.services import lista_compras_svc as svc
    loja_id, uid = _setup_loja_com_itens(app)
    with app.app_context():
        sem_anterior = svc.obter_ou_criar_semana(loja_id, data_inicio=date(2026, 5, 17))
        item = ItemListaCompras.query.filter_by(loja_id=loja_id).first()
        svc.salvar_tenho(sem_anterior, item.id, 10)
        svc.salvar_pedido_sobrou(sem_anterior, item.id, pedido=20, sobrou=4)

        h = svc.historico_anterior(loja_id, date(2026, 5, 24))
        assert h[item.id] == {'tenho': 10, 'pedido': 20, 'sobrou': 4}
        # sem semana anterior, dict vazio
        h2 = svc.historico_anterior(loja_id, date(2026, 5, 17))
        assert h2 == {}


def test_seed_idempotente(app):
    """Re-rodar o seed nao duplica."""
    from app.extensions import db
    from app.models import ItemListaCompras, Loja
    from app.seed import seed_lista_compras
    # cria as 4 lojas com os nomes que o seed espera
    with app.app_context():
        for nome in ('Ribeiro do Vale', 'Anesio Pinto Rosa', 'Nebraska', 'Industria'):
            db.session.add(Loja(nome=nome, ativa=True))
        db.session.commit()
        r1 = seed_lista_compras()
        n_total_1 = ItemListaCompras.query.count()
        assert r1['criados'] > 100      # tem bastante item nas 4 lojas
        assert not r1['lojas_faltando']
        # rerun: nada novo
        r2 = seed_lista_compras()
        assert r2['criados'] == 0
        assert r2['existentes'] == n_total_1
        assert ItemListaCompras.query.count() == n_total_1


def test_seed_loja_faltando_avisa(app):
    """Se uma loja nao existe, registra em lojas_faltando sem quebrar."""
    from app.extensions import db
    from app.models import Loja
    from app.seed import seed_lista_compras
    with app.app_context():
        db.session.add(Loja(nome='Ribeiro do Vale', ativa=True))
        db.session.commit()
        r = seed_lista_compras()
        # 3 lojas faltam (Anesio, Nebraska, Industria)
        assert set(r['lojas_faltando']) == {'Anesio Pinto Rosa', 'Nebraska', 'Industria'}
        # mas Ribeiro foi criado normalmente
        assert r['criados'] > 0


def test_rota_index_gerente_ve_propria_loja(app):
    loja_id, uid = _setup_loja_com_itens(app, papel='gerente')
    client = app.test_client()
    _login(client, uid)
    r = client.get('/lista-compras/')
    assert r.status_code == 200
    assert b'Ribeiro do Vale' in r.data
    assert b'CANELA EM PO' in r.data or b'CANELA EM P\xc3\x93' in r.data


def test_rota_salvar_json_grava_e_bloqueia_outra_loja(app):
    from app.extensions import db
    from app.models import ItemListaCompras, ListaComprasSemana, Loja, Usuario
    from app.services import lista_compras_svc as svc
    loja_id, uid = _setup_loja_com_itens(app, papel='gerente')
    with app.app_context():
        sem = svc.obter_ou_criar_semana(loja_id)
        item_id = ItemListaCompras.query.filter_by(loja_id=loja_id).first().id
        sem_id = sem.id
        # cria uma SEGUNDA loja + outro gerente
        outra = Loja(nome='Outra Loja', ativa=True)
        db.session.add(outra)
        db.session.flush()
        g2 = Usuario(login='outro_gerente', nome='Outro', papel='gerente',
                     loja_id=outra.id)
        g2.set_senha('x')
        db.session.add(g2)
        db.session.commit()
        outro_uid = g2.id

    client = app.test_client()
    _login(client, uid)
    r = client.post('/lista-compras/salvar.json', data={
        'semana_id': sem_id, 'item_id': item_id, 'tenho': '7',
    })
    assert r.status_code == 200 and r.get_json()['ok']
    with app.app_context():
        sem_fresh = ListaComprasSemana.query.get(sem_id)
        assert sem_fresh.quantidades[0].tenho == 7

    # gerente da OUTRA loja: 403
    c2 = app.test_client()
    _login(c2, outro_uid)
    r2 = c2.post('/lista-compras/salvar.json', data={
        'semana_id': sem_id, 'item_id': item_id, 'tenho': '99',
    })
    assert r2.status_code == 403


def test_rota_enviar_muda_status(app):
    from app.models import ListaComprasSemana
    from app.services import lista_compras_svc as svc
    loja_id, uid = _setup_loja_com_itens(app, papel='gerente')
    with app.app_context():
        sem = svc.obter_ou_criar_semana(loja_id)
        sem_id = sem.id
    client = app.test_client()
    _login(client, uid)
    r = client.post('/lista-compras/enviar', data={'semana_id': sem_id},
                    follow_redirects=False)
    assert r.status_code in (302, 303)
    with app.app_context():
        assert ListaComprasSemana.query.get(sem_id).status == 'enviada'


def test_consolidada_so_owner(app):
    loja_id, gerente_uid = _setup_loja_com_itens(app, papel='gerente')
    client = app.test_client()
    _login(client, gerente_uid)
    # gerente comum: 403
    assert client.get('/lista-compras/consolidada').status_code == 403


def test_consolidada_owner_200(app):
    """Outro teste pra evitar cache do Flask-Login em g — owner num client separado."""
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        owner = Usuario(login='owner_lc', nome='Owner', papel='admin', is_owner=True)
        owner.set_senha('x')
        db.session.add(owner)
        db.session.commit()
        oid = owner.id
    client = app.test_client()
    _login(client, oid)
    r = client.get('/lista-compras/consolidada')
    assert r.status_code == 200

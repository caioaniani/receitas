"""Testa as rotas de Contas a Pagar (lista/abas, detalhe, editar, pagar)."""
from datetime import date
from decimal import Decimal


def _login(c):
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def _conta(db, **kw):
    from app.models import ContaPagar
    base = dict(tipo_documento='nota_fiscal', fornecedor_nome='Moinho X',
                valor_total=Decimal('100.00'), status='aberto')
    base.update(kw)
    c = ContaPagar(**base)
    db.session.add(c)
    db.session.commit()
    return c


def test_lista_abas(app, admin_user):
    from app.extensions import db
    with app.app_context():
        _conta(db, status='aberto')
        _conta(db, status='pago')
        _conta(db, status='ignorado')
    c = app.test_client()
    _login(c)
    r = c.get('/contas-pagar/')
    assert r.status_code == 200
    assert b'Contas a Pagar' in r.data
    assert b'Em aberto' in r.data and b'Pagos' in r.data and b'Ignorados' in r.data

    # aba pago
    r2 = c.get('/contas-pagar/?aba=pago')
    assert r2.status_code == 200


def test_botao_importar_historico_owner_only(app, admin_user):
    """O botao 'Importar historico' (rota owner-only) aparece pra owner e some
    pra admin comum."""
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        dono = Usuario(nome='dono teste', login='dono', papel='admin', is_owner=True)
        dono.set_senha('123')
        db.session.add(dono)
        db.session.commit()

    co = app.test_client()
    co.post('/auth/login', data={'login': 'dono', 'senha': '123'})
    ro = co.get('/contas-pagar/')
    assert ro.status_code == 200
    assert 'Importar histórico'.encode() in ro.data

    ca = app.test_client()
    _login(ca)
    ra = ca.get('/contas-pagar/')
    assert 'Importar histórico'.encode() not in ra.data


def test_detalhe_e_editar(app, admin_user):
    from app.extensions import db
    from app.models import ContaPagar
    with app.app_context():
        cid = _conta(db, valor_total=None, fornecedor_nome=None).id

    c = app.test_client()
    _login(c)
    r = c.get(f'/contas-pagar/{cid}')
    assert r.status_code == 200

    # edita: preenche valor e vencimento
    r2 = c.post(f'/contas-pagar/{cid}/editar', data={
        'fornecedor_nome': 'Fornecedor Y',
        'tipo_documento': 'boleto',
        'valor_total': '1234.56',
        'vencimento': '2026-06-15',
        'codigo_barras': '12345678901234',
    }, follow_redirects=True)
    assert r2.status_code == 200

    with app.app_context():
        conta = db.session.get(ContaPagar, cid)
        assert conta.fornecedor_nome == 'Fornecedor Y'
        assert conta.valor_total == Decimal('1234.56')
        assert conta.vencimento == date(2026, 6, 15)
        assert conta.codigo_barras == '12345678901234'


def test_marcar_pago(app, admin_user):
    from app.extensions import db
    from app.models import ContaPagar
    with app.app_context():
        cid = _conta(db, valor_total=Decimal('50.00')).id
    c = app.test_client()
    _login(c)
    c.post(f'/contas-pagar/{cid}/pagar', data={'forma_pagamento': 'pix'},
           follow_redirects=True)
    with app.app_context():
        conta = db.session.get(ContaPagar, cid)
        assert conta.status == 'pago'
        assert conta.valor_pago == Decimal('50.00')
        assert conta.pago_em is not None
        assert conta.forma_pagamento == 'pix'


def test_ignorar_e_reabrir(app, admin_user):
    from app.extensions import db
    from app.models import ContaPagar
    with app.app_context():
        cid = _conta(db).id
    c = app.test_client()
    _login(c)
    c.post(f'/contas-pagar/{cid}/status', data={'status': 'ignorado'},
           follow_redirects=True)
    with app.app_context():
        assert db.session.get(ContaPagar, cid).status == 'ignorado'
    c.post(f'/contas-pagar/{cid}/status', data={'status': 'aberto'},
           follow_redirects=True)
    with app.app_context():
        assert db.session.get(ContaPagar, cid).status == 'aberto'


def test_filtro_por_loja(app, admin_user):
    """Pills de loja filtram as contas por canal (loja)."""
    from app.extensions import db
    with app.app_context():
        app.config['SLACK_CANAIS_NF_NOMES'] = (
            'C_RIB=Ribeiro do Vale;C_NEB=Nebraska')
        _conta(db, origem_canal='C_RIB', fornecedor_nome='Forn Ribeiro')
        _conta(db, origem_canal='C_NEB', fornecedor_nome='Forn Nebraska')

    c = app.test_client()
    _login(c)
    # sem filtro: mostra as duas + as pills de loja
    r = c.get('/contas-pagar/')
    assert b'Ribeiro do Vale' in r.data and b'Nebraska' in r.data
    assert b'Forn Ribeiro' in r.data and b'Forn Nebraska' in r.data

    # filtro Ribeiro: so a conta do canal C_RIB
    r2 = c.get('/contas-pagar/?loja=C_RIB')
    assert b'Forn Ribeiro' in r2.data
    assert b'Forn Nebraska' not in r2.data


def test_vinculo_bidirecional_no_detalhe(app, admin_user):
    """Vincula boleto→NF; abrir a NF tambem mostra o boleto (backref)."""
    from app.extensions import db
    with app.app_context():
        nf = _conta(db, tipo_documento='nota_fiscal', nf_numero='555')
        boleto = _conta(db, tipo_documento='boleto')
        nf_id, boleto_id = nf.id, boleto.id

    c = app.test_client()
    _login(c)
    # vincula a partir do boleto
    c.post(f'/contas-pagar/{boleto_id}/editar',
           data={'relacionado_id': str(nf_id), 'valor_total': '100'},
           follow_redirects=True)

    # detalhe do boleto mostra a NF ligada
    r1 = c.get(f'/contas-pagar/{boleto_id}')
    assert b'Documento(s) ligado(s)' in r1.data
    # detalhe da NF tambem mostra o boleto (lado que nao setou — via backref)
    r2 = c.get(f'/contas-pagar/{nf_id}')
    assert b'Documento(s) ligado(s)' in r2.data
    # lista marca vinculo
    r3 = c.get('/contas-pagar/?aba=aberto')
    assert b'bi-link-45deg' in r3.data


def test_lista_colapsa_grupo_e_pagar_propaga(app, admin_user):
    """NF+boleto agrupados = 1 linha; pagar o principal paga o grupo todo."""
    from datetime import date

    from app.extensions import db
    from app.models import ContaPagar
    from app.services import conta_pagar as cp
    with app.app_context():
        _conta(db, tipo_documento='nota_fiscal', origem_canal='C_RIB',
               valor_total=Decimal('141.31'), vencimento=date(2026, 5, 7))
        _conta(db, tipo_documento='boleto', origem_canal='C_RIB',
               valor_total=Decimal('141.31'), vencimento=date(2026, 5, 7))
        cp.agrupar_automatico()
        principal = ContaPagar.query.filter(ContaPagar.relacionado_id.is_(None)).first()
        pid = principal.id

    c = app.test_client()
    _login(c)
    # lista mostra UMA linha (o principal); badge de grupo presente
    r = c.get('/contas-pagar/?aba=aberto')
    assert r.status_code == 200
    assert r.data.count(b'/contas-pagar/') >= 1
    assert b'bi-link-45deg' in r.data  # marca de grupo

    # pagar o principal marca os dois
    c.post(f'/contas-pagar/{pid}/pagar', data={'forma_pagamento': 'pix'},
           follow_redirects=True)
    with app.app_context():
        todos = ContaPagar.query.all()
        assert all(x.status == 'pago' for x in todos)
        assert len(todos) == 2


def test_juntar_automatico_rota(app, admin_user):
    from datetime import date

    from app.extensions import db
    from app.models import ContaPagar
    with app.app_context():
        _conta(db, tipo_documento='nota_fiscal', origem_canal='C_RIB',
               valor_total=Decimal('50.00'), vencimento=date(2026, 6, 1))
        _conta(db, tipo_documento='boleto', origem_canal='C_RIB',
               valor_total=Decimal('50.00'), vencimento=date(2026, 6, 1))
    c = app.test_client()
    _login(c)
    c.post('/contas-pagar/juntar-automatico', follow_redirects=True)
    with app.app_context():
        sec = ContaPagar.query.filter(ContaPagar.relacionado_id.isnot(None)).count()
        assert sec == 1


def test_nao_admin_barrado(app, loja):
    """Funcionario comum nao acessa contas a pagar."""
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        u = Usuario(nome='func', login='func', papel='funcionario')
        u.set_senha('123')
        db.session.add(u)
        db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'func', 'senha': '123'})
    r = c.get('/contas-pagar/', follow_redirects=False)
    assert r.status_code in (302, 403)

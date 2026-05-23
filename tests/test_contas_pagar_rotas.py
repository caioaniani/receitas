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

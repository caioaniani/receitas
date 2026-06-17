"""Fundação da Fase 4 (pagamento Pagar.me): modelos + validação de chave.

Cobre o que NÃO move dinheiro ainda: os modelos PagamentoOnline/
PagarmeEvento (com idempotência) e o validador de chave + rota de debug.
Criação de cobrança e webhook entram na sequência (com sandbox).
"""
from decimal import Decimal
from unittest.mock import patch


def _owner(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Dono', login='dono', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


# ── Modelos ───────────────────────────────────────────────────────────

def test_pagamento_online_decimal_e_status(app):
    from app.extensions import db
    from app.models import PagamentoOnline, PedidoOnline
    with app.app_context():
        p = PedidoOnline(nome_cliente='M', email_cliente='m@x.com',
                         modo_entrega='retirada', subtotal=Decimal('50'),
                         valor_total=Decimal('50'))
        db.session.add(p)
        db.session.flush()
        pag = PagamentoOnline(pedido_id=p.id, metodo='pix',
                              valor=Decimal('50.00'))
        db.session.add(pag)
        db.session.commit()
        assert pag.status == 'pendente'  # default
        assert pag.valor == Decimal('50.00')
        # backref
        assert p.pagamentos[0].id == pag.id


def test_pagarme_evento_idempotente(app):
    """evento_id é único — reentrega do mesmo evento não duplica."""
    from sqlalchemy.exc import IntegrityError

    from app.extensions import db
    from app.models import PagarmeEvento
    with app.app_context():
        db.session.add(PagarmeEvento(evento_id='evt_1', tipo='order.paid'))
        db.session.commit()
        db.session.add(PagarmeEvento(evento_id='evt_1', tipo='order.paid'))
        import pytest
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


# ── Serviço: validação de chave ───────────────────────────────────────

def test_pagarme_sem_chave_indisponivel(app):
    from app.services import pagarme
    with app.app_context():
        app.config['PAGARME_API_KEY'] = ''
        assert pagarme.disponivel() is False
        assert pagarme.validar_chave()['ok'] is False


def test_pagarme_ambiente_pelo_prefixo(app):
    from app.services import pagarme
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_abc123'
        assert pagarme.ambiente() == 'sandbox'
        app.config['PAGARME_API_KEY'] = 'sk_live_xyz789'
        assert pagarme.ambiente() == 'producao'
        app.config['PAGARME_API_KEY'] = 'lixo'
        assert pagarme.ambiente() == 'desconhecido'


def test_pagarme_validar_chave_ok(app):
    from app.services import pagarme
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_abc'

        class FakeResp:
            status_code = 200
            text = '{}'
        with patch('app.services.pagarme.requests.get',
                   return_value=FakeResp()) as get:
            res = pagarme.validar_chave()
        assert res['ok'] is True
        assert res['ambiente'] == 'sandbox'
        # Basic auth com a chave como usuário, senha vazia
        _, kwargs = get.call_args
        assert kwargs['headers']['Authorization'].startswith('Basic ')


def test_pagarme_validar_chave_recusada(app):
    from app.services import pagarme
    with app.app_context():
        app.config['PAGARME_API_KEY'] = 'sk_test_ruim'

        class FakeResp:
            status_code = 401
            text = 'unauthorized'
        with patch('app.services.pagarme.requests.get', return_value=FakeResp()):
            res = pagarme.validar_chave()
        assert res['ok'] is False
        assert '401' in res['erro']


# ── Rota de debug (owner) ─────────────────────────────────────────────

def test_debug_pagarme_owner(app):
    c = _owner(app)
    app.config['PAGARME_API_KEY'] = 'sk_test_abc'

    class FakeResp:
        status_code = 200
        text = '{}'
    with patch('app.services.pagarme.requests.get', return_value=FakeResp()):
        r = c.get('/admin/debug-pagarme')
    assert r.status_code == 200
    data = r.get_json()
    assert data['configurado'] is True
    assert data['ambiente'] == 'sandbox'
    assert data['resultado']['ok'] is True


def test_debug_pagarme_nao_owner_bloqueado(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Ger', login='ger', papel='admin', is_owner=False)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    r = c.get('/admin/debug-pagarme')
    assert r.status_code in (302, 403)

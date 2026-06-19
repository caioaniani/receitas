"""PedidoOnline (loja própria) na tela /entregas (lista, não só o painel).

api_pedidos (que alimenta /entregas) passa a injetar os PedidoOnline do dia,
de forma independente do VNDA (aparecem mesmo se a API do VNDA cair).
"""
from decimal import Decimal
from unittest.mock import patch


def _staff(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Op', login='op', papel='admin')
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


def _pedido_online(db, codigo='ONLN01', status='pago', modo='agendada',
                   data=None):
    from app.models import Cliente, PedidoOnline
    from app.utils import hoje
    cli = Cliente(nome='Caio Cliente', email=f'{codigo.lower()}@x.com',
                  telefone='11988887777')
    db.session.add(cli)
    db.session.flush()
    p = PedidoOnline(
        codigo=codigo, cliente_id=cli.id, nome_cliente='Caio Cliente',
        email_cliente=cli.email, telefone_cliente='11988887777',
        modo_entrega=modo, status=status,
        endereco_entrega='Rua Michigan, 560, São Paulo, SP',
        endereco_cep='04571000',
        data_entrega=(data or hoje()), janela_entrega='08:00–09:00',
        subtotal=Decimal('10'), frete_valor=Decimal('5'),
        valor_total=Decimal('15'))
    db.session.add(p)
    db.session.commit()
    return p


def _api(c, data):
    with patch('app.services.vnda.buscar_pedidos_do_dia',
               return_value={'pedidos': [], 'total_janela': 0}):
        r = c.get(f'/entregas/api/pedidos?data={data}')
    assert r.status_code == 200
    return r.get_json()


def test_pedido_online_aparece_no_entregas(app):
    from app.extensions import db
    from app.utils import hoje
    c = _staff(app)
    with app.app_context():
        _pedido_online(db, codigo='ENTR01', status='pago')
        d = hoje().isoformat()
    data = _api(c, d)
    codes = [p['code'] for p in data['pedidos']]
    assert 'ENTR01' in codes
    card = next(p for p in data['pedidos'] if p['code'] == 'ENTR01')
    assert card['pedido_online'] is True
    assert 'Rua Michigan' in card['endereco']


def test_aguardando_e_cancelado_nao_aparecem(app):
    from app.extensions import db
    from app.utils import hoje
    c = _staff(app)
    with app.app_context():
        _pedido_online(db, codigo='AGUARD', status='aguardando_pagamento')
        _pedido_online(db, codigo='CANCEL', status='cancelado')
        d = hoje().isoformat()
    codes = [p['code'] for p in _api(c, d)['pedidos']]
    assert 'AGUARD' not in codes
    assert 'CANCEL' not in codes


def test_resiliente_a_vnda_fora(app):
    """VNDA caindo (exceção) NÃO esconde os pedidos da loja própria."""
    from app.extensions import db
    from app.utils import hoje
    c = _staff(app)
    with app.app_context():
        _pedido_online(db, codigo='RESIL1', status='pago')
        d = hoje().isoformat()
    with patch('app.services.vnda.buscar_pedidos_do_dia',
               side_effect=RuntimeError('VNDA 500')):
        r = c.get(f'/entregas/api/pedidos?data={d}')
    assert r.status_code == 200
    data = r.get_json()
    assert data['erro']  # aviso do VNDA presente (não-bloqueante)
    assert 'RESIL1' in [p['code'] for p in data['pedidos']]


def test_pedido_de_outro_dia_nao_aparece(app):
    from datetime import timedelta

    from app.extensions import db
    from app.utils import hoje
    c = _staff(app)
    with app.app_context():
        _pedido_online(db, codigo='AMANHA', status='pago',
                       data=hoje() + timedelta(days=1))
        d = hoje().isoformat()
    codes = [p['code'] for p in _api(c, d)['pedidos']]
    assert 'AMANHA' not in codes

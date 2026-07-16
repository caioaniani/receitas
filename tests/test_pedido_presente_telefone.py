"""Presente no site: telefone do COMPRADOR × de quem RECEBE (13/07/2026).

Caso real: cliente comprou cesta de presente e pôs o telefone da esposa
(destinatária) no campo principal; a padaria ligou pra tirar dúvida e
estragou a surpresa. O card do painel de entregas passa a separar o contato
de ENTREGA (motoboy) do COMPRADOR (dúvidas) e marca `e_presente`.
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


def _pedido(db, codigo, *, nome_dest=None, tel_dest=None, cartinha=None):
    from app.models import Cliente, PedidoOnline
    from app.utils import hoje
    cli = Cliente(nome='Ana Compradora', email=f'{codigo.lower()}@x.com',
                  telefone='11988887777')
    db.session.add(cli)
    db.session.flush()
    p = PedidoOnline(
        codigo=codigo, cliente_id=cli.id, nome_cliente='Ana Compradora',
        email_cliente=cli.email, telefone_cliente='11988887777',
        nome_destinatario=nome_dest, telefone_destinatario=tel_dest,
        cartinha=cartinha, modo_entrega='agendada', status='pago',
        endereco_entrega='Rua Michigan, 560, São Paulo, SP',
        endereco_cep='04571000', data_entrega=hoje(),
        janela_entrega='08:00–09:00', subtotal=Decimal('10'),
        frete_valor=Decimal('5'), valor_total=Decimal('15'))
    db.session.add(p)
    db.session.commit()
    return p


def _card(c, codigo):
    from app.utils import hoje
    with patch('app.services.vnda.buscar_pedidos_do_dia',
               return_value={'pedidos': [], 'total_janela': 0}):
        r = c.get(f'/entregas/api/pedidos?data={hoje().isoformat()}')
    assert r.status_code == 200
    return next(p for p in r.get_json()['pedidos'] if p['code'] == codigo)


def test_pedido_normal_nao_e_presente(app):
    from app.extensions import db
    c = _staff(app)
    with app.app_context():
        _pedido(db, 'NORM01')
    card = _card(c, 'NORM01')
    assert card['e_presente'] is False
    # sem destinatário, o contato de entrega é o próprio comprador
    assert card['telefone'] == '11988887777'
    assert card['telefone_comprador'] == '11988887777'


def test_presente_com_telefone_do_destinatario_separa_contatos(app):
    """Motoboy liga pra quem recebe (telefone); dúvida vai pro comprador."""
    from app.extensions import db
    c = _staff(app)
    with app.app_context():
        _pedido(db, 'PRES01', nome_dest='Beatriz Esposa',
                tel_dest='11970001111')
    card = _card(c, 'PRES01')
    assert card['e_presente'] is True
    assert card['destinatario'] == 'Beatriz Esposa'
    assert card['telefone'] == '11970001111'          # entrega = quem recebe
    assert card['telefone_comprador'] == '11988887777'  # dúvida = comprador


def test_presente_so_com_cartinha_marca_e_presente(app):
    """Cesta com cartinha, sem destinatário explícito: ainda é presente —
    o card avisa pra não comentar o conteúdo com quem recebe."""
    from app.extensions import db
    c = _staff(app)
    with app.app_context():
        _pedido(db, 'PRES02', cartinha='Feliz aniversário, com amor!')
    card = _card(c, 'PRES02')
    assert card['e_presente'] is True
    # sem telefone do destinatário, entrega cai no comprador (fallback)
    assert card['telefone'] == '11988887777'
    assert card['telefone_comprador'] == '11988887777'


def test_checkout_deixa_claro_telefone_do_comprador(app):
    """O formulário rotula o telefone principal como do COMPRADOR e avisa
    sobre presente (a raiz do incidente)."""
    from app.extensions import db
    from app.models import AppConfig
    # host da loja pra /loja/checkout responder
    AppConfig.set('loja_host', 'opao.online')
    db.session.commit()
    c = app.test_client()
    html = c.get('/checkout', base_url='http://opao.online').get_data(
        as_text=True)
    if 'Seu telefone' not in html:
        # host gate pode variar em teste; ao menos garante o template certo
        html = c.get('/checkout').get_data(as_text=True)
    assert 'Seu telefone (WhatsApp)' in html
    assert 'não o de quem vai receber' in html
    assert 'Telefone de quem vai receber' in html

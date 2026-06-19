"""PedidoOnline (loja própria) no painel de entregas + Lalamove (18/06/2026).

- Pedido pago do dia aparece no painel (com code = codigo, pra o Lalamove
  casar por code).
- aguardando_pagamento / cancelado NÃO aparecem; retirada aparece com flag.
- Independe do VNDA (aparece mesmo com a API VNDA fora).
- Sync: painel "entregue" → PedidoOnline.entregue + e-mail; Lalamove
  chamado → PedidoOnline.a_caminho + e-mail.
"""
from decimal import Decimal
from unittest.mock import patch


def _staff(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Op', login='op', papel='admin', is_owner=True)
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
        endereco_entrega='Rua Michigan, 560, 61A, São Paulo, SP',
        endereco_cep='04571000',
        data_entrega=(data or hoje()), janela_entrega='08:00–09:00',
        subtotal=Decimal('10'), frete_valor=Decimal('5'),
        valor_total=Decimal('15'))
    db.session.add(p)
    db.session.commit()
    return p


def _painel(c):
    """Chama /entregas/api/painel com o VNDA mockado (vazio) e devolve o JSON."""
    with patch('app.services.vnda.buscar_pedidos_do_dia',
               return_value={'pedidos': []}):
        r = c.get('/entregas/api/painel')
    assert r.status_code == 200
    return r.get_json()


def test_pedido_pago_aparece_no_painel_com_code(app):
    from app.extensions import db
    c = _staff(app)
    with app.app_context():
        _pedido_online(db, codigo='PAGO01', status='pago')
    data = _painel(c)
    codes = [p['code'] for p in data['pedidos']]
    assert 'PAGO01' in codes
    card = next(p for p in data['pedidos'] if p['code'] == 'PAGO01')
    assert card['pedido_online'] is True
    assert 'Rua Michigan' in card['endereco']   # endereço pro Lalamove
    assert card['telefone'] == '11988887777'


def test_pedido_online_aparece_uma_vez_so_no_painel(app):
    """Regressão (18/06/2026): `_injetar_pedidos_locais` passou a incluir
    PedidoOnline E o painel injetava DE NOVO em separado, transformando 1
    pedido em N cards. Agora a injeção é em um único lugar; mesmo se houver
    regressão futura, o dedupe no `api_painel` cobre."""
    from app.extensions import db
    c = _staff(app)
    with app.app_context():
        _pedido_online(db, codigo='UNQ01', status='pago')
    data = _painel(c)
    codes = [p['code'] for p in data['pedidos']]
    assert codes.count('UNQ01') == 1


def test_pedido_online_aparece_uma_vez_so_no_entregas(app):
    """Mesmo invariante na lista de /entregas (api_pedidos)."""
    from app.extensions import db
    from app.utils import hoje
    c = _staff(app)
    with app.app_context():
        _pedido_online(db, codigo='UNQ02', status='pago')
        d = hoje().isoformat()
    with patch('app.services.vnda.buscar_pedidos_do_dia',
               return_value={'pedidos': []}):
        r = c.get(f'/entregas/api/pedidos?data={d}')
    assert r.status_code == 200
    codes = [p['code'] for p in r.get_json()['pedidos']]
    assert codes.count('UNQ02') == 1


def test_aguardando_e_cancelado_nao_aparecem(app):
    from app.extensions import db
    c = _staff(app)
    with app.app_context():
        _pedido_online(db, codigo='AGUARD', status='aguardando_pagamento')
        _pedido_online(db, codigo='CANCEL', status='cancelado')
    codes = [p['code'] for p in _painel(c)['pedidos']]
    assert 'AGUARD' not in codes
    assert 'CANCEL' not in codes


def test_retirada_aparece_com_flag(app):
    from app.extensions import db
    c = _staff(app)
    with app.app_context():
        _pedido_online(db, codigo='RETIR1', status='pago', modo='retirada')
    card = next(p for p in _painel(c)['pedidos'] if p['code'] == 'RETIR1')
    assert card['retirada'] is True


def test_pedido_de_outro_dia_nao_aparece(app):
    from datetime import timedelta

    from app.extensions import db
    from app.utils import hoje
    c = _staff(app)
    with app.app_context():
        _pedido_online(db, codigo='AMANHA', status='pago',
                       data=hoje() + timedelta(days=1))
    codes = [p['code'] for p in _painel(c)['pedidos']]
    assert 'AMANHA' not in codes


def test_entregue_nao_toca_alarme(app):
    """Pedido já entregue (status PedidoOnline) entra no painel como
    'entregue' — NÃO como 'novo' (não toca o alarme da equipe)."""
    from app.extensions import db
    c = _staff(app)
    with app.app_context():
        _pedido_online(db, codigo='ENTR01', status='entregue')
    card = next(p for p in _painel(c)['pedidos'] if p['code'] == 'ENTR01')
    assert card['status'] == 'entregue'
    assert card['novo'] is False


def test_resiliente_a_vnda_fora(app):
    """VNDA caindo (exceção) NÃO esconde os pedidos da loja própria."""
    from app.extensions import db
    c = _staff(app)
    with app.app_context():
        _pedido_online(db, codigo='RESIL1', status='pago')
    with patch('app.services.vnda.buscar_pedidos_do_dia',
               side_effect=RuntimeError('VNDA 500')):
        r = c.get('/entregas/api/painel')
    assert r.status_code == 200
    data = r.get_json()
    assert data['erro']  # aviso do VNDA presente
    codes = [p['code'] for p in data['pedidos']]
    assert 'RESIL1' in codes   # mas o pedido da loja apareceu


def test_painel_entregue_sincroniza_pedido_online_e_email(app):
    from app.extensions import db
    from app.models import PedidoOnline
    c = _staff(app)
    with app.app_context():
        _pedido_online(db, codigo='SYNC01', status='pago')
    with patch('app.services.email.disponivel', return_value=True), \
         patch('app.services.email.enviar_pedido_entregue') as ev:
        r = c.post('/entregas/api/painel/status/SYNC01?status=entregue')
    assert r.status_code == 200
    ev.assert_called_once()
    with app.app_context():
        assert PedidoOnline.query.filter_by(codigo='SYNC01').first().status == 'entregue'


def test_lalamove_chamado_marca_a_caminho_e_email(app):
    from app.extensions import db
    from app.models import LalamoveEntrega, PedidoOnline
    from app.utils import hoje
    c = _staff(app)
    with app.app_context():
        _pedido_online(db, codigo='LALA01', status='pago')
        e = LalamoveEntrega(
            pedido_code='LALA01', data_ref=hoje(), status='cotacao',
            quotation_id='q1', sender_stop_id='s1', recipient_stop_id='r1',
            service_type='MOTORCYCLE',
            endereco_destino='Rua Michigan, 560',
            destinatario='Caio Cliente', telefone_destino='11988887777')
        db.session.add(e)
        db.session.commit()
        eid = e.id
    with patch('app.services.lalamove.criar_ordem',
               return_value={'ok': True, 'order_id': 'ord1',
                             'status': 'ASSIGNING_DRIVER'}), \
         patch('app.services.email.disponivel', return_value=True), \
         patch('app.services.email.enviar_pedido_a_caminho') as ev:
        r = c.post('/entregas/api/painel/lalamove/chamar',
                   json={'entrega_id': eid})
    assert r.status_code == 200 and r.get_json()['ok'] is True
    ev.assert_called_once()
    with app.app_context():
        assert PedidoOnline.query.filter_by(codigo='LALA01').first().status == 'a_caminho'


def test_sync_nao_regride_status(app):
    """Sync nunca regride: pedido já entregue não volta pra a_caminho."""
    from app.extensions import db
    from app.models import PedidoOnline
    from app.services.loja_entrega import avancar_status_entrega
    with app.app_context():
        _pedido_online(db, codigo='NOREG1', status='entregue')
        with patch('app.services.email.disponivel', return_value=True), \
             patch('app.services.email.enviar_pedido_a_caminho') as ev:
            avancar_status_entrega('NOREG1', 'a_caminho')
        ev.assert_not_called()
        assert PedidoOnline.query.filter_by(codigo='NOREG1').first().status == 'entregue'


def test_a_caminho_passa_rastreio_pro_email(app):
    """Chamar Lalamove → e-mail "a caminho" recebe o share_link de rastreio."""
    from app.extensions import db
    from app.models import LalamoveEntrega
    from app.utils import hoje
    c = _staff(app)
    with app.app_context():
        _pedido_online(db, codigo='RAST01', status='pago')
        e = LalamoveEntrega(
            pedido_code='RAST01', data_ref=hoje(), status='cotacao',
            quotation_id='q1', sender_stop_id='s1', recipient_stop_id='r1',
            service_type='MOTORCYCLE',
            endereco_destino='Rua Michigan, 560',
            destinatario='Caio Cliente', telefone_destino='11988887777')
        db.session.add(e)
        db.session.commit()
        eid = e.id
    with patch('app.services.lalamove.criar_ordem',
               return_value={'ok': True, 'order_id': 'ord1',
                             'status': 'ASSIGNING_DRIVER',
                             'share_link': 'https://share.lalamove.com/abc'}), \
         patch('app.services.email.disponivel', return_value=True), \
         patch('app.services.email.enviar_pedido_a_caminho') as ev:
        r = c.post('/entregas/api/painel/lalamove/chamar',
                   json={'entrega_id': eid})
    assert r.status_code == 200 and r.get_json()['ok'] is True
    ev.assert_called_once()
    # share_link foi propagado como rastreio_url no e-mail
    assert ev.call_args.kwargs.get('rastreio_url') == 'https://share.lalamove.com/abc'


def test_rastreio_aparece_no_corpo_do_email():
    """O template/texto de "a caminho" mostra o botão de rastreio quando há URL."""
    from types import SimpleNamespace

    from app.services.email import _template_a_caminho, _texto_a_caminho
    pedido = SimpleNamespace(
        codigo='RAST02', modo_entrega='agendada',
        endereco_entrega='Rua X, 1', data_entrega=None, janela_entrega=None)
    url = 'https://share.lalamove.com/xyz'
    html = _template_a_caminho(pedido, 'https://opao.online', rastreio_url=url)
    texto = _texto_a_caminho(pedido, 'https://opao.online', rastreio_url=url)
    assert url in html and 'Acompanhar a entrega' in html
    assert url in texto
    # sem URL não vaza link quebrado
    html_sem = _template_a_caminho(pedido, 'https://opao.online')
    assert 'Acompanhar a entrega' not in html_sem


def test_webhook_lalamove_completed_marca_entregue_e_email(app):
    """Webhook Lalamove COMPLETED → PedidoOnline entregue + e-mail automático."""
    from app.extensions import db
    from app.models import LalamoveEntrega, PedidoOnline
    from app.utils import hoje
    c = app.test_client()
    with app.app_context():
        _pedido_online(db, codigo='WHCMP1', status='a_caminho')
        e = LalamoveEntrega(
            pedido_code='WHCMP1', data_ref=hoje(), status='ON_GOING',
            order_id='ord-wh-1',
            endereco_destino='Rua Michigan, 560',
            destinatario='Caio Cliente', telefone_destino='11988887777')
        db.session.add(e)
        db.session.commit()
    with patch('app.services.lalamove._cfg', return_value='chave-secreta'), \
         patch('app.services.email.disponivel', return_value=True), \
         patch('app.services.email.enviar_pedido_entregue') as ev:
        r = c.post('/lalamove/webhook', json={
            'apiKey': 'chave-secreta',
            'eventType': 'ORDER_STATUS_CHANGED',
            'data': {'order': {'orderId': 'ord-wh-1', 'status': 'COMPLETED'}}})
    assert r.status_code == 200
    ev.assert_called_once()
    with app.app_context():
        assert PedidoOnline.query.filter_by(codigo='WHCMP1').first().status == 'entregue'


def test_webhook_lalamove_pop_nao_marca_entregue(app):
    """Regressão (bug 19/06/2026): POP_STATUS_CHANGED (proof of pickup =
    retirada) trazendo status COMPLETED NÃO pode marcar entregue. Só o evento
    oficial ORDER_STATUS_CHANGED marca — senão o pedido vira entregue na
    retirada/alocação, não na entrega ao cliente."""
    from app.extensions import db
    from app.models import LalamoveEntrega, PedidoOnline
    from app.utils import hoje
    c = app.test_client()
    with app.app_context():
        _pedido_online(db, codigo='WHPOP1', status='a_caminho')
        e = LalamoveEntrega(
            pedido_code='WHPOP1', data_ref=hoje(), status='ON_GOING',
            order_id='ord-pop-1',
            endereco_destino='Rua Michigan, 560',
            destinatario='Caio Cliente', telefone_destino='11988887777')
        db.session.add(e)
        db.session.commit()
    with patch('app.services.lalamove._cfg', return_value='chave-secreta'), \
         patch('app.services.email.disponivel', return_value=True), \
         patch('app.services.email.enviar_pedido_entregue') as ev:
        r = c.post('/lalamove/webhook', json={
            'apiKey': 'chave-secreta',
            'eventType': 'POP_STATUS_CHANGED',
            'data': {'order': {'orderId': 'ord-pop-1', 'status': 'COMPLETED'}}})
    assert r.status_code == 200
    ev.assert_not_called()                # nenhum e-mail de "entregue"
    with app.app_context():
        # continua 'a_caminho' — NÃO virou entregue por evento de retirada
        assert PedidoOnline.query.filter_by(
            codigo='WHPOP1').first().status == 'a_caminho'

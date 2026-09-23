"""Telefone internacional/inválido NUNCA vira número brasileiro inventado
(caso Cintia, 23/09/2026) + telefone de quem recebe obrigatório no presente.

Antes: `chatwoot._e164` prefixava '55' em qualquer coisa ('+1 475-292-9850'
→ '+5514752929850'), `lalamove._fone_e164` mandava o mesmo número como
contato do destinatário e o checkout descartava o '+' — a tela dizia
"template enviado" e a Meta respondia 131026 pra um desconhecido. Fonte
única agora: `app.utils.classificar_telefone`. Chatwoot, Lalamove, Z-API
e rede sempre mockados.
"""
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.extensions import db
from app.utils import TEL_BR_CELULAR, TEL_BR_FIXO, TEL_INTERNACIONAL, TEL_INVALIDO, classificar_telefone

INTERNACIONAIS = [
    ('+1 475-292-9850', '+14752929850'),       # caso real (EUA)
    ('14752929850', '+14752929850'),           # como ficou GRAVADO sem o '+'
    ('+44 20 7946 0958', '+442079460958'),     # Reino Unido
    ('+351 912 345 678', '+351912345678'),     # Portugal
    ('001 475 292 9850', '+14752929850'),      # prefixo 00 (discagem BR)
]

# Lista do test_pagarme_telefone.py — NÃO pode regredir (DDD 55 de Santa
# Maria inclusive; e o tronco/operadora do incidente 22/06/2026).
BRASILEIROS = [
    ('11964218592', TEL_BR_CELULAR, '+5511964218592', '11', '11964218592'),
    ('(11) 96421-8592', TEL_BR_CELULAR, '+5511964218592', '11', '11964218592'),
    ('5511964218592', TEL_BR_CELULAR, '+5511964218592', '11', '11964218592'),
    ('+55 11 96421-8592', TEL_BR_CELULAR, '+5511964218592', '11', '11964218592'),
    ('1136421859', TEL_BR_FIXO, '+551136421859', '11', '1136421859'),
    ('011964218592', TEL_BR_CELULAR, '+5511964218592', '11', '11964218592'),
    ('01511964218592', TEL_BR_CELULAR, '+5511964218592', '11', '11964218592'),
    ('0 15 11 96421-8592', TEL_BR_CELULAR, '+5511964218592', '11', '11964218592'),
    ('55964218592', TEL_BR_CELULAR, '+5555964218592', '55', '55964218592'),
    ('5555964218592', TEL_BR_CELULAR, '+5555964218592', '55', '55964218592'),
    ('(55) 99999-1234', TEL_BR_CELULAR, '+5555999991234', '55', '55999991234'),
    ('+55 96421-8592', TEL_BR_CELULAR, '+5555964218592', '55', '55964218592'),
    # celular no formato antigo (sem o 9): ganha o 9 da migração ANATEL
    ('1196421859', TEL_BR_CELULAR, '+5511996421859', '11', '11996421859'),
]

INVALIDOS = ['', None, '123', '999', '9999999999999999999', '99998888',
             '12345678901234567', '5511999998888 5511888887777',
             '11888887777',      # 11 dígitos sem o 9 e fora do plano NANP
             '20999998888',      # DDD 20 não existe
             '11999999999',      # 9 9999-9999 = fake
             '+5514752929850']   # o número INVENTADO de antes — nunca mais


@pytest.mark.parametrize('bruto,e164', INTERNACIONAIS)
def test_classifica_internacional(bruto, e164):
    c = classificar_telefone(bruto)
    assert c['tipo'] == TEL_INTERNACIONAL
    assert c['e164'] == e164
    assert c['ddd'] is None


@pytest.mark.parametrize('bruto,tipo,e164,ddd,nacional', BRASILEIROS)
def test_classifica_brasileiro_sem_regressao(bruto, tipo, e164, ddd, nacional):
    c = classificar_telefone(bruto)
    assert (c['tipo'], c['e164'], c['ddd'], c['nacional']) == (tipo, e164, ddd, nacional)


@pytest.mark.parametrize('bruto', INVALIDOS)
def test_classifica_invalido(bruto):
    c = classificar_telefone(bruto)
    assert c['tipo'] == TEL_INVALIDO and c['e164'] is None


# ── Pagar.me: internacional é OMITIDO (nunca +55 + número americano) ─────

@pytest.mark.parametrize('bruto', [b for b, _ in INTERNACIONAIS])
def test_pagarme_omite_internacional(bruto):
    from app.services.pagarme import _telefone_br
    assert _telefone_br(bruto) is None


def test_pagarme_ddd_55_nao_e_codigo_de_pais():
    from app.services.pagarme import _telefone_br
    assert _telefone_br('55964218592') == ('55', '964218592')
    assert _telefone_br('5555964218592') == ('55', '964218592')


# ── Chatwoot: template só pra celular BR, com o motivo explícito ─────────

def _cfg_whatsapp(app):
    app.config['CHATWOOT_URL'] = 'https://cw.example'
    app.config['CHATWOOT_API_TOKEN'] = 'tok-user'
    app.config['CHATWOOT_ACCOUNT_ID'] = '1'
    app.config['CHATWOOT_WHATSAPP_INBOX_ID'] = '7'
    app.config['CHATWOOT_WHATSAPP_TEMPLATE'] = 'duvida_pedido'


def test_motivo_sem_whatsapp(app):
    from app.services.chatwoot import _e164, motivo_telefone_sem_whatsapp
    with app.app_context():
        assert motivo_telefone_sem_whatsapp('11 99999-8888') is None
        m = motivo_telefone_sem_whatsapp('+1 475-292-9850')
        assert 'internacional' in m and 'e-mail' in m and '+14752929850' in m
        m = motivo_telefone_sem_whatsapp('11 3333-4444')
        assert 'fixo' in m and 'WhatsApp' in m
        m = motivo_telefone_sem_whatsapp('99998888')
        assert 'DDD' in m
        # _e164 NUNCA inventa: internacional/fixo/inválido → None
        assert _e164('+1 475-292-9850') is None
        assert _e164('11 3333-4444') is None
        assert _e164('(11) 99999-8888') == '+5511999998888'
        assert _e164('(55) 99999-1234') == '+5555999991234'   # DDD 55 chamável


@pytest.mark.parametrize('bruto', ['+1 475-292-9850', '+44 20 7946 0958',
                                   '14752929850', '11 3333-4444'])
def test_iniciar_conversa_recusa_sem_tocar_a_rede(app, bruto):
    from app.services import chatwoot
    with app.app_context():
        _cfg_whatsapp(app)
        with patch.object(chatwoot.requests, 'get',
                          side_effect=AssertionError('rede proibida')), \
                patch.object(chatwoot.requests, 'post',
                             side_effect=AssertionError('rede proibida')):
            res = chatwoot.iniciar_conversa_whatsapp(bruto, 'X', params=['X', 'P1'])
    assert res['ok'] is False and res['conversation_id'] is None
    assert res.get('telefone_recusado') is True
    assert 'WhatsApp' in res['erro']


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _pedido(codigo, telefone, **extra):
    from app.models import PedidoOnline
    p = PedidoOnline(codigo=codigo, status='pago', nome_cliente='Cintia',
                     telefone_cliente=telefone, email_cliente=f'{codigo.lower()}@x.com',
                     modo_entrega='agendada', subtotal=100, frete_valor=0,
                     valor_total=100, **extra)
    db.session.add(p)
    db.session.commit()
    return p


def test_chamar_cliente_internacional_400_sem_chamar_servico(app, admin_user):
    with app.app_context():
        _pedido('INTL01', '+14752929850')
    client = app.test_client()
    _login(client, admin_user)
    with patch('app.services.chatwoot.iniciar_conversa_whatsapp') as m:
        r = client.post('/entregas/api/atendimento/chamar-cliente',
                        json={'codigo': 'INTL01'})
    assert r.status_code == 400
    d = r.get_json()
    assert d['ok'] is False and d['telefone_recusado'] is True
    assert 'internacional' in d['erro'] and 'e-mail' in d['erro']
    assert m.call_count == 0


def test_chamar_cliente_numero_gravado_sem_mais_tambem_recusa(app, admin_user):
    """Pedido antigo gravou '14752929850' (o checkout descartava o '+')."""
    with app.app_context():
        _pedido('INTL02', '14752929850')
    client = app.test_client()
    _login(client, admin_user)
    with patch('app.services.chatwoot.iniciar_conversa_whatsapp') as m:
        r = client.post('/entregas/api/atendimento/chamar-cliente',
                        json={'codigo': 'INTL02'})
    assert r.status_code == 400 and m.call_count == 0
    assert 'internacional' in r.get_json()['erro']


def test_chamar_telefone_e_motorista_recusam_internacional(app, admin_user):
    from app.models import LalamoveEntrega
    with app.app_context():
        e = LalamoveEntrega(pedido_code='PED9', order_id='ord-PED9',
                            status='ON_GOING', motorista_nome='Joao',
                            motorista_telefone='+351 912 345 678')
        db.session.add(e)
        db.session.commit()
        eid = e.id
    client = app.test_client()
    _login(client, admin_user)
    with patch('app.services.chatwoot.iniciar_conversa_whatsapp') as m:
        r = client.post('/entregas/api/atendimento/chamar-telefone',
                        json={'telefone': '+44 20 7946 0958', 'nome': 'X'})
        assert r.status_code == 400 and 'internacional' in r.get_json()['erro']
        r = client.post('/entregas/api/atendimento/chamar-telefone',
                        json={'telefone': '11 3333-4444', 'nome': 'X'})
        assert r.status_code == 400 and 'fixo' in r.get_json()['erro']
        r = client.post('/entregas/api/atendimento/chamar-motorista',
                        json={'entrega_id': eid})
        assert r.status_code == 400 and 'internacional' in r.get_json()['erro']
    assert m.call_count == 0


def test_chamar_telefone_ddd_55_passa(app, admin_user):
    """DDD 55 (Santa Maria) sem código de país era recusado como 'sem DDD'
    pelo _e164 antigo — cliente gaúcho não podia ser chamado."""
    client = app.test_client()
    _login(client, admin_user)
    with patch('app.services.chatwoot.iniciar_conversa_whatsapp',
               return_value={'ok': True, 'conversation_id': 1, 'nova': True,
                             'erro': None, 'aberta': True}) as m:
        r = client.post('/entregas/api/atendimento/chamar-telefone',
                        json={'telefone': '(55) 99999-1234', 'nome': 'X'})
    assert r.status_code == 200 and m.call_count == 1


# ── Lalamove: internacional NÃO vai como contato do destinatário ──────────

def _config_lala(app):
    app.config['LALAMOVE_API_KEY'] = 'pk_test_chave'
    app.config['LALAMOVE_API_SECRET'] = 'sk_test_segredo'
    app.config['LALAMOVE_REMETENTE_FONE'] = '11999990000'


def test_fone_e164_lalamove_nunca_inventa():
    from app.services.lalamove import _fone_e164
    assert _fone_e164('+1 475-292-9850') is None
    assert _fone_e164('14752929850') is None
    assert _fone_e164('(11) 99999-0000') == '+5511999990000'
    assert _fone_e164('11 3333-4444') == '+551133334444'     # fixo serve pra corrida
    assert _fone_e164('(55) 99999-1234') == '+5555999991234'


@pytest.mark.parametrize('telefone,trecho', [
    ('+1 475-292-9850', 'internacional (+14752929850)'),
    ('14752929850', 'internacional'),
    ('', 'sem telefone do destinatário'),
    ('123', 'inválido'),
])
def test_criar_ordem_internacional_usa_filial_e_registra(app, telefone, trecho):
    from app.services import lalamove
    _config_lala(app)
    with app.app_context(), \
            patch('app.services.lalamove._request',
                  return_value=(201, {'data': {'orderId': 'O-1',
                                               'status': 'ASSIGNING_DRIVER'}})) as req:
        r = lalamove.criar_ordem('Q1', 'S0', 'S1', 'Cintia', telefone,
                                 observacao='Pedido ABC — O Pão')
    assert r['ok'] is True
    dest = req.call_args.args[2]['data']['recipients'][0]
    assert dest['phone'] == '+5511999990000'        # filial, nunca inventado
    assert 'Pedido ABC — O Pão' in dest['remarks']
    assert trecho in dest['remarks']
    assert trecho in r['aviso']
    assert '+5514752929850' not in dest['remarks']


def test_criar_ordem_brasileiro_segue_como_contato(app):
    from app.services import lalamove
    _config_lala(app)
    with app.app_context(), \
            patch('app.services.lalamove._request',
                  return_value=(201, {'data': {'orderId': 'O-2',
                                               'status': 'ASSIGNING_DRIVER'}})) as req:
        r = lalamove.criar_ordem('Q1', 'S0', 'S1', 'Maria', '(55) 99999-1234',
                                 observacao='Pedido DEF')
    dest = req.call_args.args[2]['data']['recipients'][0]
    assert dest['phone'] == '+5555999991234'
    assert dest['remarks'] == 'Pedido DEF'
    assert r['aviso'] is None


def test_rota_chamar_repassa_aviso(app, admin_user):
    from app.models import LalamoveEntrega
    _config_lala(app)
    with app.app_context():
        e = LalamoveEntrega(pedido_code='VND-9', quotation_id='Q', sender_stop_id='S0',
                            recipient_stop_id='S1', status='cotacao',
                            destinatario='Cintia', telefone_destino='+14752929850')
        db.session.add(e)
        db.session.commit()
        eid = e.id
    c = app.test_client()
    _login(c, admin_user)
    ordem = {'ok': True, 'order_id': 'O-9', 'status': 'ASSIGNING_DRIVER',
             'share_link': None, 'valor': None, 'moeda': 'BRL',
             'aviso': 'Telefone do destinatário é internacional (+14752929850) — '
                      'sem contato local; falar com a padaria.'}
    with patch('app.services.lalamove.criar_ordem', return_value=ordem):
        d = c.post('/entregas/api/painel/lalamove/chamar',
                   json={'entrega_id': eid}).get_json()
    assert d['ok'] is True and 'internacional' in d['aviso']


# ── Checkout: guarda o '+' do internacional; presente exige telefone ─────

def test_checkout_normaliza_preservando_internacional():
    from app.services.loja_checkout import _normalizar_telefone_checkout as n
    assert n('+1 475-292-9850') == ('+14752929850', None)
    assert n('(11) 98888-7777') == ('11988887777', None)
    assert n('55 96421-8592') == ('55964218592', None)        # DDD 55 fica BR
    assert n('+55 11 98888-7777') == ('5511988887777', None)  # BR com DDI: dígitos
    assert n('') == ('', None)


def _form_presente(loja, data, **extra):
    from tests.test_loja_checkout_v2 import _END_NF
    f = {'nome': 'Maria', 'sobrenome': 'Silva', 'email': 'm@x.com',
         'cpf': '52998224725', 'aceite_lgpd': '1',
         'modo_entrega': 'retirada', 'loja_id': str(loja.id),
         'data_entrega': data, 'janela_entrega': '08:00–09:00',
         'e_presente': '1', 'nome_destinatario': 'Ana Pereira', **_END_NF}
    f.update(extra)
    return f


def test_presente_exige_telefone_de_quem_recebe(app):
    from app.services import loja_checkout
    from tests.test_loja_checkout_v2 import _loja, _produto
    with app.app_context():
        p = _produto(db)
        loja = _loja(db)
        base = datetime(2026, 6, 17, 10, 0)
        data = loja_checkout.datas_disponiveis('retirada', base=base)[1].isoformat()
        pedido, erros = loja_checkout.criar_pedido(
            _form_presente(loja, data), [{'kind': 'produto', 'id': p.id, 'qtd': 1}],
            base=base)
        assert pedido is None
        assert any('telefone de quem vai receber' in e.lower() for e in erros)
        # com o telefone, passa — e o internacional é guardado com '+'
        pedido, erros = loja_checkout.criar_pedido(
            _form_presente(loja, data, telefone_destinatario='+1 475-292-9850'),
            [{'kind': 'produto', 'id': p.id, 'qtd': 1}], base=base)
        assert erros == []
        assert pedido.telefone_destinatario == '+14752929850'


def test_checkout_html_marca_telefone_de_quem_recebe_obrigatorio(app, monkeypatch):
    from tests.test_loja_checkout_v2 import _admin
    monkeypatch.delenv('LOJA_VISIVEL', raising=False)
    c = _admin(app)
    html = c.get('/loja/checkout').get_data(as_text=True)
    assert 'Telefone de quem vai receber' in html
    assert '(opcional)' not in html.split('Telefone de quem vai receber')[1][:60]


# ── Painel: selo do tipo e aviso de presente sem telefone ────────────────

def _staff(app):
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


def _pedido_painel(codigo, tel_cliente, nome_dest=None, tel_dest=None):
    from app.models import PedidoOnline
    from app.utils import hoje
    p = PedidoOnline(
        codigo=codigo, nome_cliente='Ana', email_cliente=f'{codigo.lower()}@x.com',
        telefone_cliente=tel_cliente, nome_destinatario=nome_dest,
        telefone_destinatario=tel_dest, modo_entrega='agendada', status='pago',
        endereco_entrega='Rua Michigan, 560', endereco_cep='04571000',
        data_entrega=hoje(), janela_entrega='08:00–09:00',
        subtotal=Decimal('10'), frete_valor=Decimal('5'), valor_total=Decimal('15'))
    db.session.add(p)
    db.session.commit()


def _cards(c):
    from app.utils import hoje
    with patch('app.services.vnda.buscar_pedidos_do_dia',
               return_value={'pedidos': [], 'total_janela': 0}):
        r = c.get(f'/entregas/api/pedidos?data={hoje().isoformat()}')
    assert r.status_code == 200
    return {p['code']: p for p in r.get_json()['pedidos']}


def test_painel_expoe_tipo_do_telefone_e_presente_sem_telefone(app):
    with app.app_context():
        c = _staff(app)
        _pedido_painel('INT1', '+14752929850')
        _pedido_painel('PRE1', '11988887777', nome_dest='Bia')
        _pedido_painel('PRE2', '11988887777', nome_dest='Bia', tel_dest='11 3333-4444')
        _pedido_painel('BR1', '11988887777')
    cards = _cards(c)
    assert cards['INT1']['telefone_tipo'] == 'internacional'
    assert cards['INT1']['telefone_comprador_tipo'] == 'internacional'
    assert cards['PRE1']['sem_telefone_destinatario'] is True
    assert cards['PRE1']['e_presente'] is True
    assert cards['PRE2']['sem_telefone_destinatario'] is False
    assert cards['PRE2']['telefone_tipo'] == 'br_fixo'
    assert cards['PRE2']['telefone_comprador_tipo'] == 'br_celular'
    assert cards['BR1']['telefone_tipo'] == 'br_celular'
    assert cards['BR1']['sem_telefone_destinatario'] is False


def test_painel_html_tem_selo_e_aviso(app):
    with app.app_context():
        c = _staff(app)
    html = c.get('/entregas/painel-testes').get_data(as_text=True)
    assert 'sem WhatsApp, use e-mail' in html
    assert 'sem_telefone_destinatario' in html

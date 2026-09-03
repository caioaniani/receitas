"""Integração Lalamove: assinatura HMAC v3, cotação/ordem (API mockada),
rotas do painel (cotar -> chamar -> cancelar) e webhook de status."""
import hashlib
import hmac as hmac_mod
import json
from unittest.mock import patch

from app.extensions import db


def _config(app):
    app.config['LALAMOVE_API_KEY'] = 'pk_test_chave'
    app.config['LALAMOVE_API_SECRET'] = 'sk_test_segredo'
    app.config['LALAMOVE_REMETENTE_FONE'] = '11999990000'
    app.config['LALAMOVE_ORIGEM_LATLNG'] = '-23.6095,-46.6907'


def _login(c):
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def test_assinatura_hmac_v3(app):
    """Authorization: hmac KEY:ts:HMAC_SHA256(secret, ts\\r\\nMETODO\\r\\npath\\r\\n\\r\\nbody)."""
    from app.services import lalamove
    _config(app)
    with app.app_context(), \
         patch('app.services.lalamove.requests.request',
               return_value=_Resp(200, {'data': {}})) as req, \
         patch('app.services.lalamove.time.time', return_value=1718000000.0):
        lalamove._request('POST', '/v3/quotations', {'data': {'x': 1}})
    kwargs = req.call_args[1]
    auth = kwargs['headers']['Authorization']
    assert auth.startswith('hmac pk_test_chave:1718000000000:')
    corpo = json.dumps({'data': {'x': 1}}, ensure_ascii=False)
    raw = f'1718000000000\r\nPOST\r\n/v3/quotations\r\n\r\n{corpo}'
    esperado = hmac_mod.new(b'sk_test_segredo', raw.encode(),
                            hashlib.sha256).hexdigest()
    assert auth.endswith(esperado)
    assert kwargs['headers']['Market'] == 'BR'


def test_cotar_monta_stops_e_parseia(app):
    from app.services import lalamove
    _config(app)
    resposta = {'data': {
        'quotationId': 'Q1', 'expiresAt': '2026-06-10T20:00:00Z',
        'priceBreakdown': {'total': '24.50', 'currency': 'BRL'},
        'distance': {'value': '4970', 'unit': 'm'},
        'stops': [{'stopId': 'S0'}, {'stopId': 'S1'}],
    }}
    with app.app_context(), \
         patch('app.services.frete.geocodificar_entrega',
               return_value=(-23.62, -46.70, 'Rua X')) as geo, \
         patch('app.services.lalamove.requests.request',
               return_value=_Resp(201, resposta)) as req:
        lalamove._origem_cache = None
        r = lalamove.cotar('Rua X, 100 - Moema', 'moto')
    assert r['ok'] is True
    assert (r['quotation_id'], r['valor'], r['distancia_m']) == ('Q1', '24.50', 4970)
    assert (r['sender_stop_id'], r['recipient_stop_id']) == ('S0', 'S1')
    payload = json.loads(req.call_args[1]['data'])
    assert payload['data']['serviceType'] == 'LALAGO'   # moto no BR
    assert len(payload['data']['stops']) == 2
    # origem fixada por env (sem geocodificar a origem; so o destino)
    assert geo.call_count == 1


def test_cotar_erro_da_api_vira_mensagem(app):
    from app.services import lalamove
    _config(app)
    erro = {'errors': [{'id': 'ERR_INVALID_MARKET', 'message': 'market errado'}]}
    with app.app_context(), \
         patch('app.services.frete.geocodificar_entrega',
               return_value=(-23.62, -46.70, 'Rua X')), \
         patch('app.services.lalamove.requests.request',
               return_value=_Resp(422, erro)):
        lalamove._origem_cache = None
        r = lalamove.cotar('Rua X', 'carro')
    assert r['ok'] is False
    assert 'ERR_INVALID_MARKET' in r['erro']


def test_cotar_destino_sem_coordenada_alerta_dono_e_sensor(app):
    """Geocode do destino falha: a corrida não sai — sensor no painel + WhatsApp
    ao dono na hora (decisão do dono 09/07/2026)."""
    from app.services import lalamove
    _config(app)
    with app.app_context(), \
         patch('app.services.frete.geocodificar_entrega', return_value=None), \
         patch('app.services.frete_sensor.registrar') as sensor, \
         patch('app.services.loja_alerta.alertar_endereco_falho') as alerta:
        lalamove._origem_cache = None
        r = lalamove.cotar('Endereço fantasma sem mapa', 'moto')
    assert r['ok'] is False
    assert sensor.called and sensor.call_args.args[1] == 'lalamove_falhou'
    assert alerta.called
    assert alerta.call_args.kwargs.get('motivo') == 'lalamove'


def test_fone_e164():
    from app.services.lalamove import _fone_e164
    assert _fone_e164('(11) 99999-0000') == '+5511999990000'
    assert _fone_e164('5511999990000') == '+5511999990000'
    assert _fone_e164('') is None


def test_fluxo_painel_cotar_chamar_cancelar(app, admin_user):
    from app.models import LalamoveEntrega
    _config(app)
    c = app.test_client()
    _login(c)

    cot = {'ok': True, 'quotation_id': 'Q9', 'valor': '31.00', 'moeda': 'BRL',
           'distancia_m': 8200, 'sender_stop_id': 'S0',
           'recipient_stop_id': 'S1', 'service_type': 'CAR',
           'expira_em': 'x'}
    with patch('app.services.lalamove.cotar', return_value=cot):
        r = c.post('/entregas/api/painel/lalamove/cotar', json={
            'code': 'VND-777', 'endereco': 'Rua Y, 200 - Pinheiros',
            'destinatario': 'Maria', 'telefone': '11 98888-7777',
            'veiculo': 'carro'})
    d = r.get_json()
    assert d['ok'] is True and d['valor'] == '31.00' and d['distancia'] == '8.2 km'
    assert d['veiculo'] == '🚗 Carro'   # rotulo legivel, nao slug
    eid = d['entrega_id']
    with app.app_context():
        e = db.session.get(LalamoveEntrega, eid)
        assert e.status == 'cotacao' and e.pedido_code == 'VND-777'
        assert str(e.valor) == '31.00'

    ordem = {'ok': True, 'order_id': 'O-123', 'status': 'ASSIGNING_DRIVER',
             'share_link': 'https://share.lalamove.com/x', 'valor': '31.00',
             'moeda': 'BRL'}
    with patch('app.services.lalamove.criar_ordem', return_value=ordem):
        r2 = c.post('/entregas/api/painel/lalamove/chamar',
                    json={'entrega_id': eid})
    d2 = r2.get_json()
    assert d2['ok'] is True
    assert d2['lalamove']['status'] == 'ASSIGNING_DRIVER'
    assert d2['lalamove']['pode_cancelar'] is True

    # reuso da mesma cotacao nao pode
    with patch('app.services.lalamove.criar_ordem', return_value=ordem):
        assert c.post('/entregas/api/painel/lalamove/chamar',
                      json={'entrega_id': eid}).status_code == 400

    with patch('app.services.lalamove.cancelar', return_value={'ok': True}):
        r3 = c.post('/entregas/api/painel/lalamove/cancelar',
                    json={'entrega_id': eid})
    assert r3.get_json()['lalamove']['status'] == 'CANCELED'


def test_api_painel_inclui_lalamove(app, admin_user):
    from app.models import LalamoveEntrega
    from app.utils import hoje
    _config(app)
    with app.app_context():
        db.session.add(LalamoveEntrega(
            pedido_code='VND-1', data_ref=hoje(), order_id='O-9',
            status='PICKED_UP', service_type='LALAGO', valor=12,
            moeda='BRL', share_link='https://share/x'))
        db.session.commit()
    c = app.test_client()
    _login(c)
    pedidos_fake = [{'code': 'VND-1', 'destinatario': 'Ana',
                     'endereco': 'Rua Z', 'itens': []}]
    with patch('app.blueprints.entregas.routes._painel_pedidos_do_dia',
               return_value=(pedidos_fake, None)):
        d = c.get('/entregas/api/painel').get_json()
    lala = d['pedidos'][0]['lalamove']
    assert lala['status'] == 'PICKED_UP'
    assert lala['rotulo'] == 'Saiu para entrega'
    assert lala['veiculo'] == '🏍 Moto'


def test_webhook_atualiza_status_e_motorista(app):
    from app.models import LalamoveEntrega
    from app.utils import hoje
    _config(app)
    with app.app_context():
        db.session.add(LalamoveEntrega(
            pedido_code='VND-2', data_ref=hoje(), order_id='O-55',
            status='ASSIGNING_DRIVER'))
        db.session.commit()
    c = app.test_client()

    # GET = teste de alcance (o portal valida com non-POST)
    assert c.get('/lalamove/webhook').status_code == 200

    # apiKey errada -> 401, nada muda
    r = c.post('/lalamove/webhook', json={
        'apiKey': 'pk_outra', 'eventType': 'ORDER_STATUS_CHANGED',
        'data': {'order': {'orderId': 'O-55', 'status': 'PICKED_UP'}}})
    assert r.status_code == 401

    r2 = c.post('/lalamove/webhook', json={
        'apiKey': 'pk_test_chave', 'eventType': 'ORDER_STATUS_CHANGED',
        'data': {'order': {'orderId': 'O-55', 'status': 'PICKED_UP',
                           'shareLink': 'https://share/55'},
                 'driver': {'name': 'Carlos M', 'phone': '+5511977776666'}}})
    assert r2.status_code == 200
    with app.app_context():
        e = LalamoveEntrega.query.filter_by(order_id='O-55').one()
        assert e.status == 'PICKED_UP'
        assert e.motorista_nome == 'Carlos M'
        assert e.share_link == 'https://share/55'

    # ordem desconhecida -> 200 ignorado (retry da Lalamove nao fica preso)
    r3 = c.post('/lalamove/webhook', json={
        'apiKey': 'pk_test_chave',
        'data': {'order': {'orderId': 'O-INEXISTENTE', 'status': 'COMPLETED'}}})
    assert r3.status_code == 200


def test_debug_lalamove_owner(app):
    """Rota de diagnostico (owner): prefixos sem vazar segredo + teste no
    endpoint neutro /v3/cities pra separar 'credencial ruim' de 'payload'."""
    from app.models import Usuario
    _config(app)
    with app.app_context():
        u = Usuario(nome='Dono', login='dono9', papel='admin', is_owner=True)
        u.set_senha('x' * 8)
        db.session.add(u)
        db.session.commit()
        uid = u.id
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True

    with patch('app.services.lalamove._request',
               return_value=(200, {'data': [{'locode': 'BR SAO'}]})):
        d = c.get('/admin/debug-lalamove').get_json()
    assert d['teste_cities_ok'] is True
    assert d['cidades'] == ['BR SAO']
    assert d['key_prefixo'].startswith('pk_test_')
    assert 'sk_test_segredo' not in str(d)   # segredo nunca vaza inteiro

    with patch('app.services.lalamove._request',
               return_value=(401, {'errors': [{'id': 'ERR_AUTHENTICATE'}]})):
        d2 = c.get('/admin/debug-lalamove').get_json()
    assert d2['teste_cities_ok'] is False
    assert 'ERR_AUTHENTICATE' in d2['teste_cities_corpo']


def test_cotar_aceita_nome_direto_da_api(app):
    """O seletor do painel manda o nome oficial (VAN, LALAPRO...) — alem dos
    apelidos moto/carro mantidos pra chamadas antigas."""
    from app.services import lalamove
    _config(app)
    resposta = {'data': {'quotationId': 'Q2',
                         'priceBreakdown': {'total': '80.00', 'currency': 'BRL'},
                         'stops': [{'stopId': 'A'}, {'stopId': 'B'}]}}
    with app.app_context(), \
         patch('app.services.frete.geocodificar_entrega',
               return_value=(-23.62, -46.70, 'Rua X')), \
         patch('app.services.lalamove.requests.request',
               return_value=_Resp(201, resposta)) as req:
        lalamove._origem_cache = None
        r = lalamove.cotar('Rua X, 100', 'van')
    assert r['ok'] is True and r['service_type'] == 'VAN'
    assert json.loads(req.call_args[1]['data'])['data']['serviceType'] == 'VAN'

    with app.app_context():
        assert lalamove.cotar('Rua X', 'jetski')['erro'].startswith('veículo inválido')


def test_webhook_probe_sem_apikey_recebe_200_e_registra_hit(app):
    """O teste de URL do portal manda POST sem apiKey/evento — precisa de
    200 (senao o portal acusa falha). Nao autoriza nada: evento real exige
    apiKey valida + orderId conhecido. Todo hit fica registrado pro debug."""
    import os

    from app.blueprints.lalamove import routes as wh
    _config(app)
    if os.path.exists(wh.ARQUIVO_ULTIMO_HIT):
        os.remove(wh.ARQUIVO_ULTIMO_HIT)
    c = app.test_client()

    r = c.post('/lalamove/webhook', json={})
    assert r.status_code == 200 and r.get_json()['ping'] is True
    hit = wh.ultimo_hit()
    assert hit['tipo'] == 'ping' and hit['tinha_apikey'] is False

    assert c.get('/lalamove/webhook').status_code == 200
    assert wh.ultimo_hit()['tipo'] == 'alcance'

    # apiKey ERRADA com evento continua 401
    r2 = c.post('/lalamove/webhook', json={
        'apiKey': 'pk_invasor', 'data': {'order': {'orderId': 'O-1'}}})
    assert r2.status_code == 401


def test_webhook_saldo_carteira_e_painel(app, admin_user):
    """WALLET_BALANCE_CHANGED grava o saldo (linha unica) e o api_painel
    expoe pro atendente ver antes de chamar."""
    from app.models import LalamoveSaldo
    _config(app)
    c = app.test_client()
    r = c.post('/lalamove/webhook', json={
        'apiKey': 'pk_test_chave', 'eventType': 'WALLET_BALANCE_CHANGED',
        'data': {'balance': {'amount': '152.30', 'currency': 'BRL'}}})
    assert r.status_code == 200
    with app.app_context():
        s = db.session.get(LalamoveSaldo, 1)
        assert str(s.valor) == '152.30' and s.moeda == 'BRL'

    # segundo evento sobrescreve a MESMA linha
    c.post('/lalamove/webhook', json={
        'apiKey': 'pk_test_chave', 'eventType': 'WALLET_BALANCE_CHANGED',
        'data': {'balance': {'amount': '99.00', 'currency': 'BRL'}}})
    with app.app_context():
        assert LalamoveSaldo.query.count() == 1
        assert str(db.session.get(LalamoveSaldo, 1).valor) == '99.00'

    _login(c)
    with patch('app.blueprints.entregas.routes._painel_pedidos_do_dia',
               return_value=([], None)):
        d = c.get('/entregas/api/painel').get_json()
    assert d['lalamove_saldo'] == '99.00'


# ── Priority fee / gorjeta pra acelerar entregador (15/06/2026) ──

def test_adicionar_priority_fee_monta_payload_e_parseia(app):
    """Service: POST /v3/orders/{id}/priority-fee com data.priorityFee.
    Resposta traz o novo priceBreakdown (priorityFee + total)."""
    from app.services import lalamove as lala_svc
    _config(app)
    resp = {'data': {'priceBreakdown': {
        'priorityFee': '5.00', 'total': '36.00', 'currency': 'BRL'}}}
    with app.app_context():
        with patch('app.services.lalamove._request',
                   return_value=(200, resp)) as req:
            r = lala_svc.adicionar_priority_fee('O-123', 5)
    assert r['ok'] is True
    assert r['priority_fee'] == '5.00'
    assert r['total'] == '36.00'
    # confere endpoint + payload enviados
    metodo, path = req.call_args[0][0], req.call_args[0][1]
    payload = req.call_args[0][2]
    assert metodo == 'POST'
    assert path == '/v3/orders/O-123/priority-fee'
    assert payload == {'data': {'priorityFee': '5.00'}}


def test_adicionar_priority_fee_valor_invalido_nao_chama_api(app):
    from app.services import lalamove as lala_svc
    _config(app)
    with app.app_context():
        with patch('app.services.lalamove._request') as req:
            assert lala_svc.adicionar_priority_fee('O-1', 0)['ok'] is False
            assert lala_svc.adicionar_priority_fee('O-1', -3)['ok'] is False
            assert lala_svc.adicionar_priority_fee('O-1', 'abc')['ok'] is False
        req.assert_not_called()


def test_adicionar_priority_fee_erro_da_api_vira_mensagem(app):
    """Se a API recusar (ex: campo errado), o erro cru chega pro atendente."""
    from app.services import lalamove as lala_svc
    _config(app)
    corpo = {'errors': [{'id': 'ERR_INVALID', 'message': 'bad field'}]}
    with app.app_context():
        with patch('app.services.lalamove._request', return_value=(422, corpo)):
            r = lala_svc.adicionar_priority_fee('O-9', 5)
    assert r['ok'] is False
    assert 'bad field' in r['erro']


def _criar_corrida_assigning(app, admin_user):
    """Helper: corrida já chamada (ASSIGNING_DRIVER) pronta pra acelerar."""
    from app.models import LalamoveEntrega
    from app.utils import hoje
    with app.app_context():
        e = LalamoveEntrega(
            pedido_code='VND-900', data_ref=hoje(), order_id='O-900',
            status='ASSIGNING_DRIVER', service_type='LALAGO',
            valor=20, moeda='BRL', criado_por_id=admin_user.id)
        db.session.add(e)
        db.session.commit()
        return e.id


def test_acelerar_rota_adiciona_gorjeta(app, admin_user):
    """Fluxo feliz: atendente dá R$5, a API aceita, persistimos priority_fee
    e o novo total volta no card."""
    from app.models import LalamoveEntrega
    _config(app)
    eid = _criar_corrida_assigning(app, admin_user)
    c = app.test_client()
    _login(c)
    resp = {'ok': True, 'priority_fee': '5.00', 'total': '25.00', 'moeda': 'BRL'}
    with patch('app.services.lalamove.adicionar_priority_fee',
               return_value=resp) as svc:
        r = c.post('/entregas/api/painel/lalamove/acelerar',
                   json={'entrega_id': eid, 'valor': 5})
    d = r.get_json()
    assert d['ok'] is True
    assert d['lalamove']['priority_fee'] == '5.00'
    assert d['lalamove']['valor'] == '25.00'   # total atualizado
    svc.assert_called_once_with('O-900', 5.0)
    with app.app_context():
        e = db.session.get(LalamoveEntrega, eid)
        assert str(e.priority_fee) == '5.00'


def test_acelerar_exige_valor_maior_que_o_atual(app, admin_user):
    """A Lalamove substitui (não soma) e exige valor maior. Barramos antes
    de bater na API com mensagem amigável."""
    from app.extensions import db as _db
    from app.models import LalamoveEntrega
    _config(app)
    eid = _criar_corrida_assigning(app, admin_user)
    with app.app_context():
        e = _db.session.get(LalamoveEntrega, eid)
        e.priority_fee = 10
        _db.session.commit()
    c = app.test_client()
    _login(c)
    with patch('app.services.lalamove.adicionar_priority_fee') as svc:
        r = c.post('/entregas/api/painel/lalamove/acelerar',
                   json={'entrega_id': eid, 'valor': 8})
    assert r.status_code == 400
    assert 'maior' in r.get_json()['erro']
    svc.assert_not_called()


def test_acelerar_so_quando_procura_entregador(app, admin_user):
    """Corrida que já tem motorista (ON_GOING) ou encerrou não pode acelerar."""
    from app.extensions import db as _db
    from app.models import LalamoveEntrega
    _config(app)
    eid = _criar_corrida_assigning(app, admin_user)
    with app.app_context():
        e = _db.session.get(LalamoveEntrega, eid)
        e.status = 'ON_GOING'
        _db.session.commit()
    c = app.test_client()
    _login(c)
    with patch('app.services.lalamove.adicionar_priority_fee') as svc:
        r = c.post('/entregas/api/painel/lalamove/acelerar',
                   json={'entrega_id': eid, 'valor': 5})
    assert r.status_code == 400
    svc.assert_not_called()
    # E o JSON do card reflete que NÃO dá mais pra acelerar
    with app.app_context():
        from app.blueprints.entregas.routes import _lalamove_json
        e = _db.session.get(LalamoveEntrega, eid)
        assert _lalamove_json(e)['pode_acelerar'] is False


def test_acelerar_valor_invalido_400(app, admin_user):
    _config(app)
    eid = _criar_corrida_assigning(app, admin_user)
    c = app.test_client()
    _login(c)
    with patch('app.services.lalamove.adicionar_priority_fee') as svc:
        assert c.post('/entregas/api/painel/lalamove/acelerar',
                      json={'entrega_id': eid, 'valor': 0}).status_code == 400
        assert c.post('/entregas/api/painel/lalamove/acelerar',
                      json={'entrega_id': eid, 'valor': 'xx'}).status_code == 400
    svc.assert_not_called()

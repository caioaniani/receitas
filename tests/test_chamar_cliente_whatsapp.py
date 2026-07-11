"""Botão "Chamar cliente pelo WhatsApp" do painel de entregas (11/07/2026).

Fora da janela de 24h a Meta só deixa a empresa iniciar conversa com TEMPLATE
aprovado. O fluxo: acha/cria contato pelo telefone -> reusa conversa aberta
OU cria uma nova + manda o template -> devolve conversation_id pro painel
abrir a conversa na direita. Testa a orquestração (requests mockado) e o
endpoint /entregas/api/atendimento/chamar-cliente.
"""
from unittest.mock import MagicMock, patch


def _cfg_whatsapp(app):
    app.config['CHATWOOT_URL'] = 'https://cw.example'
    app.config['CHATWOOT_API_TOKEN'] = 'tok-user'
    app.config['CHATWOOT_ACCOUNT_ID'] = '1'
    app.config['CHATWOOT_WHATSAPP_INBOX_ID'] = '7'
    app.config['CHATWOOT_WHATSAPP_TEMPLATE'] = 'duvida_pedido'
    app.config['CHATWOOT_WHATSAPP_TEMPLATE_LANG'] = 'pt_BR'


def _resp(status, payload):
    r = MagicMock()
    r.status_code = status
    r.text = '{}'
    r.json.return_value = payload
    return r


# ── _e164 ────────────────────────────────────────────────────────────────

def test_e164(app):
    from app.services.chatwoot import _e164
    with app.app_context():
        assert _e164('(11) 99999-8888') == '+5511999998888'
        assert _e164('5511999998888') == '+5511999998888'
        assert _e164('11 3333-4444') == '+551133334444'
        assert _e164('99998888') is None     # sem DDD -> None
        assert _e164('') is None


# ── Orquestração: conversa NOVA (cria contato + conversa + template) ───────

def test_inicia_conversa_nova_manda_template(app):
    from app.services import chatwoot
    with app.app_context():
        _cfg_whatsapp(app)
        chamadas = {'template': None}

        def fake_get(url, **kw):
            if '/contacts/search' in url:
                return _resp(200, {'payload': []})           # não achou contato
            if url.endswith('/conversations'):               # conversas do contato
                return _resp(200, {'payload': []})           # nenhuma aberta
            return _resp(404, {})

        def fake_post(url, json=None, **kw):
            if url.endswith('/contacts'):
                return _resp(200, {'payload': {'contact': {
                    'id': 55,
                    'contact_inboxes': [{'inbox': {'id': 7}, 'source_id': 'src-7'}],
                }}})
            if url.endswith('/conversations'):
                assert json['source_id'] == 'src-7'
                assert json['inbox_id'] == 7
                assert json['contact_id'] == 55
                return _resp(200, {'id': 900})
            if '/messages' in url:
                chamadas['template'] = json
                return _resp(200, {'id': 1})
            return _resp(404, {})

        with patch.object(chatwoot.requests, 'get', side_effect=fake_get), \
                patch.object(chatwoot.requests, 'post', side_effect=fake_post):
            res = chatwoot.iniciar_conversa_whatsapp(
                '11999998888', 'Simone', params=['Simone', 'ABC123'])

        assert res['ok'] is True
        assert res['conversation_id'] == 900
        assert res['nova'] is True
        tp = chamadas['template']['template_params']
        assert tp['name'] == 'duvida_pedido'
        assert tp['language'] == 'pt_BR'
        assert tp['processed_params'] == {'1': 'Simone', '2': 'ABC123'}
        # content renderizado (exibição na thread — sem ele o balão fica vazio
        # em versões do Chatwoot; a Meta recebe o template aprovado)
        assert 'Simone' in chamadas['template']['content']
        assert 'ABC123' in chamadas['template']['content']


# ── Orquestração: REUSA conversa aberta e MANDA o template mesmo assim ─────

def test_reusa_conversa_aberta_e_manda_template(app):
    """Conversa 'aberta' no Chatwoot não significa janela de 24h aberta na
    Meta (fix 11/07/2026: o skip do template em conversa reusada deixava o
    cliente sem receber NADA). O template vai SEMPRE."""
    from app.services import chatwoot
    with app.app_context():
        _cfg_whatsapp(app)
        posts = []

        def fake_get(url, **kw):
            if '/contacts/search' in url:
                return _resp(200, {'payload': [{
                    'id': 55, 'phone_number': '+5511999998888',
                    'contact_inboxes': [{'inbox': {'id': 7}, 'source_id': 'src-7'}],
                }]})
            if url.endswith('/conversations'):
                return _resp(200, {'payload': [
                    {'id': 901, 'inbox_id': 7, 'status': 'open',
                     'last_activity_at': 10}]})
            return _resp(404, {})

        def fake_post(url, json=None, **kw):
            posts.append((url, json))
            return _resp(200, {'id': 1})

        with patch.object(chatwoot.requests, 'get', side_effect=fake_get), \
                patch.object(chatwoot.requests, 'post', side_effect=fake_post):
            res = chatwoot.iniciar_conversa_whatsapp(
                '11999998888', 'Simone', params=['Simone', 'ABC123'])

        assert res['ok'] is True
        assert res['conversation_id'] == 901
        assert res['nova'] is False
        # NÃO criou conversa nova, mas mandou o template na 901
        assert len(posts) == 1
        url, body = posts[0]
        assert '/conversations/901/messages' in url
        assert body['template_params']['name'] == 'duvida_pedido'


# ── Guardas: desconfigurado e telefone inválido ───────────────────────────

def test_desconfigurado_devolve_erro(app):
    from app.services import chatwoot
    with app.app_context():
        app.config['CHATWOOT_URL'] = 'https://cw.example'
        app.config['CHATWOOT_API_TOKEN'] = 'tok'
        app.config['CHATWOOT_ACCOUNT_ID'] = '1'
        app.config['CHATWOOT_WHATSAPP_INBOX_ID'] = ''      # falta inbox
        app.config['CHATWOOT_WHATSAPP_TEMPLATE'] = ''
        res = chatwoot.iniciar_conversa_whatsapp('11999998888', 'X', params=[])
        assert res['ok'] is False
        assert 'configurado' in res['erro']


def test_telefone_sem_ddd_recusa(app):
    from app.services import chatwoot
    with app.app_context():
        _cfg_whatsapp(app)
        res = chatwoot.iniciar_conversa_whatsapp('99998888', 'X', params=[])
        assert res['ok'] is False
        assert 'invalid' in res['erro'].lower() or 'ddd' in res['erro'].lower()


# ── Endpoint /entregas/api/atendimento/chamar-cliente ─────────────────────

def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _pedido_online(codigo='ABC123', telefone='11999998888'):
    from app.extensions import db
    from app.models import PedidoOnline
    p = PedidoOnline(codigo=codigo, status='pago', nome_cliente='Simone',
                     telefone_cliente=telefone, email_cliente='s@example.com',
                     modo_entrega='agendada', subtotal=100, frete_valor=0,
                     valor_total=100)
    db.session.add(p)
    db.session.commit()
    return p


def test_endpoint_chama_e_devolve_conv_id(app, admin_user):
    with app.app_context():
        _pedido_online('ABC123')
    client = app.test_client()
    _login(client, admin_user)
    with patch('app.services.chatwoot.iniciar_conversa_whatsapp',
               return_value={'ok': True, 'conversation_id': 900,
                             'nova': True, 'erro': None}) as m:
        r = client.post('/entregas/api/atendimento/chamar-cliente',
                        json={'codigo': 'ABC123'})
    assert r.status_code == 200
    d = r.get_json()
    assert d['ok'] is True and d['conversation_id'] == 900 and d['nome'] == 'Simone'
    # passou telefone + nome + params [nome, codigo] pro serviço
    args, kwargs = m.call_args
    assert args[0] == '11999998888' and args[1] == 'Simone'
    assert kwargs['params'] == ['Simone', 'ABC123']


def test_endpoint_pedido_inexistente_404(app, admin_user):
    client = app.test_client()
    _login(client, admin_user)
    r = client.post('/entregas/api/atendimento/chamar-cliente',
                    json={'codigo': 'NAOEXISTE'})
    assert r.status_code == 404
    assert r.get_json()['ok'] is False


def test_endpoint_sem_telefone_400(app, admin_user):
    with app.app_context():
        _pedido_online('SEMTEL', telefone='')
    client = app.test_client()
    _login(client, admin_user)
    r = client.post('/entregas/api/atendimento/chamar-cliente',
                    json={'codigo': 'SEMTEL'})
    assert r.status_code == 400
    assert 'telefone' in r.get_json()['erro'].lower()


def test_endpoint_falha_do_servico_vira_502(app, admin_user):
    """Template falhou mas a conversa foi criada: devolve 502 + conv_id (o
    painel abre a conversa e mostra o erro)."""
    with app.app_context():
        _pedido_online('ABC123')
    client = app.test_client()
    _login(client, admin_user)
    with patch('app.services.chatwoot.iniciar_conversa_whatsapp',
               return_value={'ok': False, 'conversation_id': 900,
                             'nova': True, 'erro': 'HTTP 422: template'}):
        r = client.post('/entregas/api/atendimento/chamar-cliente',
                        json={'codigo': 'ABC123'})
    assert r.status_code == 502
    d = r.get_json()
    assert d['ok'] is False and d['conversation_id'] == 900
    assert 'template' in d['erro']


def test_inboxes_owner_pass(app, owner_user):
    client = app.test_client()
    _login(client, owner_user)
    with patch('app.services.chatwoot.listar_inboxes',
               return_value=[{'id': 7, 'nome': 'WhatsApp', 'canal': 'Channel::Whatsapp'}]):
        r = client.get('/entregas/api/atendimento/chatwoot-inboxes')
    assert r.status_code == 200
    assert r.get_json()['inboxes'][0]['id'] == 7


def test_inboxes_admin_barrado(app, admin_user):
    client = app.test_client()
    _login(client, admin_user)          # admin não-owner
    assert client.get(
        '/entregas/api/atendimento/chatwoot-inboxes').status_code == 403

"""Bot de atendimento (Agent Bot do Chatwoot) — Fase 1.

Cobre o cerebro (chatbot.responder, com Claude mockado) e o webhook
(filtros anti-loop + handoff), com chatbot/chatwoot mockados.
"""
from types import SimpleNamespace
from unittest.mock import patch


def _resp_texto(texto):
    return SimpleNamespace(content=[SimpleNamespace(type='text', text=texto)])


def _resp_tool(mensagem_cliente, motivo='quer humano'):
    blk = SimpleNamespace(type='tool_use', name='transferir_para_humano',
                          input={'mensagem_cliente': mensagem_cliente, 'motivo': motivo})
    return SimpleNamespace(content=[blk])


# ── cérebro ──

def test_responder_texto(app):
    from app.services import chatbot
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic') as M:
            M.return_value.messages.create.return_value = _resp_texto('Olá! Bem-vindo à O Pão. 🥖')
            r = chatbot.responder([{'role': 'user', 'content': 'oi'}])
    assert r['acao'] == 'responder'
    assert 'Pão' in r['texto']


def test_responder_handoff_via_tool(app):
    from app.services import chatbot
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic') as M:
            M.return_value.messages.create.return_value = _resp_tool('Já te passo pra um atendente!')
            r = chatbot.responder([{'role': 'user', 'content': 'quero falar com uma pessoa'}])
    assert r['acao'] == 'handoff'
    assert 'atendente' in r['texto'].lower()
    assert r['motivo']


def test_responder_sem_api_key_faz_handoff(app):
    from app.services import chatbot
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = ''
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': ''}):
            r = chatbot.responder([{'role': 'user', 'content': 'oi'}])
    assert r['acao'] == 'handoff'


# ── webhook ──

def _post(client, **payload_over):
    payload = {'event': 'message_created', 'message_type': 'incoming',
               'conversation': {'id': 7, 'status': 'pending'}, 'content': 'oi'}
    payload.update(payload_over)
    return client.post('/crm/bot?k=seg', json=payload)


def test_bot_webhook_responde(app):
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    client = app.test_client()
    with patch('app.services.chatwoot.buscar_historico',
               return_value=[{'role': 'user', 'content': 'oi'}]), \
         patch('app.services.chatbot.responder',
               return_value={'acao': 'responder', 'texto': 'Olá!'}), \
         patch('app.services.chatwoot.enviar_mensagem', return_value={'ok': True}) as env, \
         patch('app.services.chatwoot.definir_status', return_value={'ok': True}) as st:
        r = _post(client)
    assert r.status_code == 200
    assert r.get_json()['acao'] == 'responder'
    env.assert_called_once()
    st.assert_not_called()


def test_bot_webhook_handoff_muda_status(app):
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    client = app.test_client()
    with patch('app.services.chatwoot.buscar_historico',
               return_value=[{'role': 'user', 'content': 'quero humano'}]), \
         patch('app.services.chatbot.responder',
               return_value={'acao': 'handoff', 'texto': 'Chamando atendente', 'motivo': 'x'}), \
         patch('app.services.chatwoot.enviar_mensagem', return_value={'ok': True}) as env, \
         patch('app.services.chatwoot.definir_status', return_value={'ok': True}) as st:
        r = _post(client, content='quero humano')
    assert r.status_code == 200
    assert r.get_json()['acao'] == 'handoff'
    env.assert_called_once()
    st.assert_called_once_with(7, 'open')


def test_bot_webhook_ignora_outgoing(app):
    """Mensagem do proprio bot/atendente nao dispara o bot (anti-loop)."""
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    client = app.test_client()
    with patch('app.services.chatbot.responder') as resp:
        r = _post(client, message_type='outgoing')
    assert r.status_code == 200
    assert r.get_json()['ignorado'] == 'nao-incoming'
    resp.assert_not_called()


def test_bot_webhook_ignora_conversa_aberta(app):
    """Conversa ja 'open' (humano assumiu) — bot nao responde."""
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    client = app.test_client()
    with patch('app.services.chatbot.responder') as resp:
        r = _post(client, conversation={'id': 7, 'status': 'open'})
    assert r.status_code == 200
    assert r.get_json()['ignorado'] == 'nao-pending'
    resp.assert_not_called()


def test_bot_webhook_token_invalido(app):
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    client = app.test_client()
    r = client.post('/crm/bot?k=errado', json={'event': 'message_created'})
    assert r.status_code == 403

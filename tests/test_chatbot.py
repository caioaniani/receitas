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


class _SyncThread:
    """Thread fake que roda o target na hora — pro webhook assíncrono virar
    síncrono no teste."""

    def __init__(self, target=None, daemon=None, **kw):
        self._target = target

    def start(self):
        if self._target:
            self._target()


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
    with patch('threading.Thread', _SyncThread), \
         patch('app.services.chatwoot.buscar_historico',
               return_value=[{'role': 'user', 'content': 'oi'}]), \
         patch('app.services.chatbot.responder',
               return_value={'acao': 'responder', 'texto': 'Olá!'}), \
         patch('app.services.chatwoot.enviar_mensagem', return_value={'ok': True}) as env, \
         patch('app.services.chatwoot.definir_status', return_value={'ok': True}) as st:
        r = _post(client)
    assert r.status_code == 200
    assert r.get_json()['ok'] is True
    env.assert_called_once()
    st.assert_not_called()


def test_bot_webhook_handoff_muda_status(app):
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    client = app.test_client()
    with patch('threading.Thread', _SyncThread), \
         patch('app.services.chatwoot.buscar_historico',
               return_value=[{'role': 'user', 'content': 'quero humano'}]), \
         patch('app.services.chatbot.responder',
               return_value={'acao': 'handoff', 'texto': 'Chamando atendente', 'motivo': 'x'}), \
         patch('app.services.chatwoot.enviar_mensagem', return_value={'ok': True}) as env, \
         patch('app.services.chatwoot.definir_status', return_value={'ok': True}) as st:
        r = _post(client, content='quero humano')
    assert r.status_code == 200
    assert r.get_json()['ok'] is True
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


# ── Fase 2: ferramentas ──

def test_responder_loop_consultar_produtos(app):
    """Claude chama consultar_produtos, recebe o resultado e responde."""
    from app.services import chatbot
    tool_blk = SimpleNamespace(type='tool_use', name='consultar_produtos',
                               id='t1', input={'busca': 'croissant'})
    resp1 = SimpleNamespace(content=[tool_blk])
    resp2 = _resp_texto('🥐 Croissant Almond — R$32,50')
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic') as M, \
             patch('app.services.bot_tools.consultar_produtos',
                   return_value={'produtos': [{'nome': 'Croissant Almond', 'sku': '10007',
                                               'preco': 32.5, 'disponivel': True}]}) as cp:
            M.return_value.messages.create.side_effect = [resp1, resp2]
            r = chatbot.responder([{'role': 'user', 'content': 'tem croissant de amêndoas?'}])
    assert r['acao'] == 'responder'
    assert 'Croissant' in r['texto']
    cp.assert_called_once()


def test_gerar_link_carrinho():
    from app.services import bot_tools
    r = bot_tools.gerar_link_carrinho([{'sku': '10007', 'qtd': 2}, {'sku': '10009', 'qtd': 1}])
    assert r['link'].endswith('/carrinho?itens=10007:2,10009:1')


def test_gerar_link_carrinho_vazio():
    from app.services import bot_tools
    assert 'erro' in bot_tools.gerar_link_carrinho([])


def test_consultar_produtos_parse(app):
    from app.services import bot_tools
    bot_tools._catalogo_cache.clear()
    fake = SimpleNamespace(json=lambda: {'products': [
        {'name': 'Croissant Almond', 'available': True,
         'variants': [{'sku': '10007', 'price': 32.5, 'available': True}]}]})
    with app.app_context():
        with patch('app.services.vnda._get', return_value=fake):
            r = bot_tools.consultar_produtos('croissant')
    assert r['produtos'][0]['sku'] == '10007'
    assert r['produtos'][0]['disponivel'] is True


def test_consultar_produtos_variants_dict(app):
    """VNDA devolve variants como DICT keyed por id — o parser lê o sku certo,
    nunca o id do produto nem o id da variante."""
    from app.services import bot_tools
    bot_tools._catalogo_cache.clear()
    fake = SimpleNamespace(json=lambda: {'products': [
        {'id': 10, 'name': 'Box Mimo', 'available': True,
         'variants': {'11': {'sku': '10007', 'price': 166.0, 'available': True}}}]})
    with app.app_context():
        with patch('app.services.vnda._get', return_value=fake):
            r = bot_tools.consultar_produtos('box mimo')
    assert 'erro' not in r
    p = r['produtos'][0]
    assert p['sku'] == '10007'   # nunca '10' (produto) nem '11' (variante)
    assert p['preco'] == 166.0


def test_consultar_produtos_vnda_fora_retorna_erro(app):
    """VNDA fora -> {'erro'}, pra o bot passar pro humano (nunca inventar)."""
    from app.services import bot_tools
    bot_tools._catalogo_cache.clear()
    with app.app_context():
        with patch('app.services.vnda._get', return_value=None):
            r = bot_tools.consultar_produtos('cesta')
    assert 'erro' in r


def test_responder_erro_produtos_forca_handoff(app):
    """Se consultar_produtos falha, o bot NUNCA repassa preço de memória —
    força handoff (salvaguarda de dinheiro)."""
    from app.services import chatbot
    tool_blk = SimpleNamespace(type='tool_use', name='consultar_produtos',
                               id='t1', input={'busca': 'cesta'})
    resp1 = SimpleNamespace(content=[tool_blk])
    # Claude "tentaria" listar preços de memória — o guard tem que barrar.
    resp2 = _resp_texto('Box Mimo — R$166\nBonjour — R$215')
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic') as M, \
             patch('app.services.bot_tools.consultar_produtos',
                   return_value={'erro': 'VNDA indisponível no momento'}):
            M.return_value.messages.create.side_effect = [resp1, resp2]
            r = chatbot.responder([{'role': 'user', 'content': 'quero uma cesta'}])
    assert r['acao'] == 'handoff'
    assert 'R$' not in r['texto']   # não vaza preço inventado

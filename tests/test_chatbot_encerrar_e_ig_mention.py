"""Bot extrapolando (16/06/2026, 2 casos do dono):

(1) Cliente fechou com 'obrigada' e o bot continuou puxando conversa →
    fix: tool `encerrar_conversa` SEM enviar mensagem, muda status pra
    `resolved` no Chatwoot. Cliente reabre mandando outra msg, bot atende.

(2) Marcação em story do Instagram caiu como conversa pendente e o bot
    tentou 'atender' a story → fix: detector no /crm/bot que faz handoff
    silencioso (status 'open') sem chamar o Claude.
"""
from types import SimpleNamespace
from unittest.mock import patch

# ── Fix 1: encerrar_conversa ────────────────────────────────────────────

def test_tool_encerrar_registrada():
    """Trava: sem isso o LLM vê 'encerrar_conversa' no prompt mas a
    chamada quebra com 'ferramenta desconhecida'."""
    from app.services import chatbot
    nomes = [t['name'] for t in chatbot.TOOLS]
    assert 'encerrar_conversa' in nomes


def test_resp_encerrar_devolve_silencio():
    """A acao 'encerrar' tem texto vazio (NÃO manda nada pro cliente).
    Diferente do handoff, que MANDA o aviso de horario antes de transferir."""
    from app.services import chatbot
    r = chatbot._resp_encerrar('teste')
    assert r['acao'] == 'encerrar'
    assert r['texto'] == ''
    assert r['motivo'] == 'teste'


def test_responder_propaga_encerrar_quando_bot_chama_a_tool(app, monkeypatch):
    """Integracao: se o LLM retorna tool_use=encerrar_conversa, o `responder`
    devolve {'acao': 'encerrar', 'texto': ''} — sem alucinar despedida."""
    from app.services import chatbot
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-x')

    blk = SimpleNamespace(type='tool_use', name='encerrar_conversa',
                          id='t1', input={})
    fake_resp = SimpleNamespace(content=[blk])

    class FakeMsgs:
        def create(self, **kw):
            return fake_resp
    class FakeClient:
        def __init__(self, **kw): pass
        messages = FakeMsgs()
    monkeypatch.setattr('anthropic.Anthropic', FakeClient)

    with app.app_context():
        out = chatbot.responder([
            {'role': 'assistant', 'content': 'Aqui está seu link: ...'},
            {'role': 'user', 'content': 'obrigada!'},
        ])
    assert out['acao'] == 'encerrar'
    assert out['texto'] == ''
    assert 'encerrar_conversa' in out.get('tools_usadas', [])


def test_crm_routes_resolve_conversa_quando_bot_encerra(app):
    """Quando `responder` devolve acao='encerrar', o webhook /crm/bot
    NÃO chama enviar_mensagem E muda o status do Chatwoot pra 'resolved'.
    """
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    app.config['CHATWOOT_URL'] = 'https://atendimento.x.com'
    app.config['CHATWOOT_ACCOUNT_ID'] = '1'
    app.config['CHATWOOT_BOT_TOKEN'] = 'bot-tok'
    c = app.test_client()
    payload = {
        'event': 'message_created',
        'id': 5001,
        'message_type': 'incoming',
        'content': 'obrigada!',
        'conversation': {'id': 777, 'status': 'pending'},
        'sender': {'name': 'Cliente'},
    }
    with patch('app.services.chatbot.responder',
                return_value={'acao': 'encerrar', 'texto': '',
                              'motivo': 'encerramento'}) as resp, \
         patch('app.services.chatbot.carregar_historico', return_value=None), \
         patch('app.services.chatwoot.buscar_historico', return_value=[]), \
         patch('app.services.chatbot.salvar_historico'), \
         patch('app.services.chatwoot.enviar_mensagem') as enviar, \
         patch('app.services.chatwoot.definir_status') as status, \
         patch('app.services.chatbot_vigia.disponivel', return_value=False):
        r = c.post('/crm/bot?k=seg', json=payload)
        # webhook responde ack na hora; processamento eh em thread separada
        # — o test_client da pra esperar o thread terminar com join, mas o
        # padrao deste arquivo eh dar tempo pelo proximo step. Aqui vamos
        # esperar via fim do thread implicito (join via Thread em coleta):
    assert r.status_code == 200
    # da tempo pra o thread daemon rodar (chamadas mockadas, ~ms)
    import time as _time
    _time.sleep(0.2)
    resp.assert_called_once()
    enviar.assert_not_called()  # silêncio absoluto
    status.assert_called_with(777, 'resolved')


def test_prompt_tem_secao_FECHAMENTO_e_3_condicoes():
    """O LLM precisa ver a regra no prompt — sem isso ele não chama a
    tool. Travamos as 3 condições EXPLICITAS pra evitar 'fechamento' em
    casos com pendência (CPF, opções, confirmação)."""
    from app.services.chatbot_prompt import PROMPT
    assert 'FECHAMENTO' in PROMPT
    assert 'encerrar_conversa' in PROMPT
    # As 3 condições rigidas (caso o LLM seja mais agressivo, pelo menos
    # o conteúdo pra interpretar tá explícito)
    assert 'TURNO ANTERIOR' in PROMPT or 'turno anterior' in PROMPT
    assert 'pendência' in PROMPT.lower()
    # Os padrões de fechamento típicos aparecem
    for p in ('obrigada', 'valeu', 'tchau'):
        assert p in PROMPT.lower(), f'falta o padrão "{p}" no prompt'


# ── Fix 2: story mention do Instagram ──────────────────────────────────

def test_detector_pega_message_type_explicito():
    from app.blueprints.crm.routes import _e_story_mention_instagram
    p = {'content_attributes': {'message_type': 'story_mention'}}
    assert _e_story_mention_instagram(p, {})


def test_detector_pega_in_reply_to_story():
    from app.blueprints.crm.routes import _e_story_mention_instagram
    p = {'content_attributes': {'in_reply_to_external_source_id': 'ig_reel_98'}}
    assert _e_story_mention_instagram(p, {})


def test_detector_pega_ig_vazio_com_cdn_instagram():
    """Fallback observacional: inbox IG + content vazio + anexo do CDN do IG."""
    from app.blueprints.crm.routes import _e_story_mention_instagram
    p = {'content': '',
         'attachments': [{'data_url': 'https://scontent-gru.cdninstagram.com/x.jpg'}]}
    conv = {'meta': {'channel': {'channel_type': 'Channel::Instagram'}}}
    assert _e_story_mention_instagram(p, conv)


def test_detector_NAO_falso_positivo_cliente_real_com_foto():
    """Cliente real mandando foto + texto no IG DM → não é story mention."""
    from app.blueprints.crm.routes import _e_story_mention_instagram
    p = {'content': 'oi, quanto custa esse pão?',
         'attachments': [{'data_url': 'https://scontent.cdninstagram.com/x.jpg'}]}
    conv = {'meta': {'channel': {'channel_type': 'Channel::Instagram'}}}
    assert not _e_story_mention_instagram(p, conv)


def test_detector_NAO_falso_positivo_whatsapp_normal():
    """WhatsApp normal → não dispara."""
    from app.blueprints.crm.routes import _e_story_mention_instagram
    p = {'content': 'oi', 'attachments': []}
    conv = {'meta': {'channel': {'channel_type': 'Channel::Api'}}}
    assert not _e_story_mention_instagram(p, conv)


def test_webhook_NAO_chama_bot_quando_e_story_mention(app):
    """Integração: story mention chega → status vira 'open' e o bot NÃO é
    chamado. NUNCA gasta token de Claude, nunca posta mensagem."""
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    app.config['CHATWOOT_URL'] = 'https://atendimento.x.com'
    app.config['CHATWOOT_ACCOUNT_ID'] = '1'
    app.config['CHATWOOT_BOT_TOKEN'] = 'bot-tok'
    c = app.test_client()
    payload = {
        'event': 'message_created',
        'id': 6001,
        'message_type': 'incoming',
        'content': '',
        'content_attributes': {'message_type': 'story_mention'},
        'conversation': {'id': 888, 'status': 'pending'},
    }
    with patch('app.services.chatbot.responder') as resp_bot, \
         patch('app.services.chatwoot.definir_status') as status:
        r = c.post('/crm/bot?k=seg', json=payload)
    assert r.status_code == 200
    body = r.get_json()
    assert body['ignorado'] == 'ig-story-mention'
    assert 'story_mention' in body['motivo']
    resp_bot.assert_not_called()  # NÃO chamou o Claude
    status.assert_called_with(888, 'open')   # equipe decide


def test_webhook_continua_normal_pra_msg_de_cliente_real_com_foto(app):
    """Regressão do anti-padrão (cliente IG mandando foto de produto +
    perguntando preço). Webhook deve seguir o fluxo normal (bot é chamado)."""
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    c = app.test_client()
    payload = {
        'event': 'message_created',
        'id': 7001,
        'message_type': 'incoming',
        'content': 'quanto custa esse?',
        'attachments': [{'file_type': 'image',
                          'data_url': 'https://scontent.cdninstagram.com/x.jpg'}],
        'conversation': {'id': 999, 'status': 'pending',
                         'meta': {'channel': {'channel_type': 'Channel::Instagram'}}},
    }
    # Não mockamos o bot — só checamos que NÃO disparou story_mention.
    with patch('app.services.chatbot.responder',
                return_value={'acao': 'responder', 'texto': 'oi'}), \
         patch('app.services.chatwoot.enviar_mensagem'), \
         patch('app.services.chatwoot.definir_status'), \
         patch('app.services.chatbot.carregar_historico', return_value=None), \
         patch('app.services.chatwoot.buscar_historico', return_value=[]), \
         patch('app.services.chatbot.salvar_historico'), \
         patch('app.services.chatbot_vigia.disponivel', return_value=False):
        r = c.post('/crm/bot?k=seg', json=payload)
    body = r.get_json()
    assert body.get('ignorado') != 'ig-story-mention'  # NÃO foi descartado

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


def test_vigia_registra_no_historico(app):
    """`avaliar` (wrapper) grava o resultado em `_historico` pra /admin/vigia/diag."""
    from app.services import chatbot_vigia
    veredicto = {'alerta': True, 'gravidade': 'alta',
                 'motivo': 'X', 'acao_sugerida': ''}
    with app.app_context():
        chatbot_vigia._historico.clear()
        app.config['CHATBOT_VIGIA'] = True
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        with patch('app.services.chatbot_vigia._chamar_haiku',
                   return_value=veredicto), \
             patch('app.services.chatbot_vigia._resumo_estoque_loja',
                   return_value=''), \
             patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}):
            chatbot_vigia.avaliar([{'role': 'user', 'content': 'tem croissant?'}],
                                  conv_id=99, nome_contato='Maria')
        ultimos = chatbot_vigia.ultimos()
    assert len(ultimos) == 1
    assert ultimos[0]['conv_id'] == 99
    assert ultimos[0]['gravidade'] == 'alta'
    assert ultimos[0]['enviado'] is True
    assert 'croissant' in ultimos[0]['mensagem_cliente']


def test_vigia_disparar_teste_cenario_estoque(app):
    """Cenario sintetico 'estoque' chega ao Haiku + Z-API + grava historico."""
    from app.services import chatbot_vigia
    veredicto = {'alerta': True, 'gravidade': 'alta',
                 'motivo': 'TESTE: bot esgotado mas tem na loja',
                 'acao_sugerida': 'atualizar VNDA'}
    with app.app_context():
        chatbot_vigia._historico.clear()
        app.config['CHATBOT_VIGIA'] = True
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        with patch('app.services.chatbot_vigia._chamar_haiku',
                   return_value=veredicto), \
             patch('app.services.chatbot_vigia._resumo_estoque_loja',
                   return_value=''), \
             patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as send:
            r = chatbot_vigia.disparar_teste('estoque')
    assert r['enviado'] is True
    send.assert_called_once()
    assert 'teste-estoque' in str(chatbot_vigia.ultimos()[0]['conv_id'])


def test_admin_vigia_diag_route_responde_json(app):
    from app.extensions import db
    from app.models import Usuario
    from app.services import chatbot_vigia
    with app.app_context():
        chatbot_vigia._historico.clear()
        app.config['CHATBOT_VIGIA'] = True
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        owner = Usuario(nome='owner', login='owner', papel='admin', is_owner=True)
        owner.set_senha('123')
        db.session.add(owner)
        db.session.commit()
        owner_id = owner.id
    client = app.test_client()
    with client.session_transaction() as s:
        s['_user_id'] = str(owner_id)
    r = client.get('/admin/vigia/diag')
    assert r.status_code == 200
    j = r.get_json()
    assert j['ligado'] is True
    assert j['numero_destino'] == '5511999990000'
    assert j['anthropic_api_key_configurada'] is True
    assert j['ultimos_veredictos'] == []


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


def test_consultar_pedido_retorna_data_agendada(app):
    """consultar_pedido devolve a data AGENDADA (extra.DataDeEntrega), nunca o
    expected_delivery_date bugado do VNDA (o do "entregue hoje")."""
    from app.services import bot_tools
    order = {
        'code': 'AB123', 'status': 'paid', 'total': 321.0,
        'items': [{'product_name': 'Family Box', 'quantity': 1}],
        'extra': {'DataDeEntrega': '08/06/2026', 'Periodo': '08-09h'},
        'expected_delivery_date': '2026-06-05',  # bug do VNDA: "hoje"
    }
    with app.app_context():
        with patch('app.services.vnda.buscar_pedido_completo', return_value=order):
            r = bot_tools.consultar_pedido('AB123')
    assert r['data_entrega'] == '08/06/2026'   # a agendada, não a bugada
    assert r['periodo'] == '08-09h'
    assert r['numero'] == 'AB123'


def test_consultar_pedido_nao_encontrado(app):
    from app.services import bot_tools
    with app.app_context():
        with patch('app.services.vnda.buscar_pedido_completo', return_value=None):
            r = bot_tools.consultar_pedido('XYZ')
    assert 'erro' in r


def test_consultar_produtos_inclui_descricao(app):
    """Match focado traz a descrição (conteúdo da cesta), com HTML limpo."""
    from app.services import bot_tools
    bot_tools._catalogo_cache.clear()
    fake = SimpleNamespace(json=lambda: {'products': [
        {'name': 'Cesta Monamour', 'available': True,
         'description': '<p>Contém: 2 croissants, 1 geleia, suco de laranja</p>',
         'variants': [{'sku': '999', 'price': 200.0, 'available': True}]}]})
    with app.app_context():
        with patch('app.services.vnda._get', return_value=fake):
            r = bot_tools.consultar_produtos('monamour')
    p = r['produtos'][0]
    assert 'croissants' in p['descricao']
    assert '<p>' not in p['descricao']   # HTML removido


def test_consultar_produtos_fallback_sem_descricao(app):
    """Sem match no nome, o catálogo amplo vem SEM descrição (token-light)."""
    from app.services import bot_tools
    bot_tools._catalogo_cache.clear()
    fake = SimpleNamespace(json=lambda: {'products': [
        {'name': 'Pão Sourdough', 'available': True,
         'description': 'Pão de fermentação natural',
         'variants': [{'sku': '111', 'price': 30.0, 'available': True}]}]})
    with app.app_context():
        with patch('app.services.vnda._get', return_value=fake):
            r = bot_tools.consultar_produtos('zzznadacasa')
    p = r['produtos'][0]
    assert 'descricao' not in p   # stripado no fallback
    assert p['sku'] == '111'


def test_vigia_media_nao_pinga_so_registra(app):
    """Gravidade media NÃO manda WhatsApp na hora (vai pro resumo); só registra."""
    from app.services import chatbot_vigia
    veredicto = {'alerta': True, 'gravidade': 'media',
                 'motivo': 'handoff evitável (conteúdo de cesta)', 'acao_sugerida': ''}
    with app.app_context():
        chatbot_vigia._historico.clear()
        app.config['CHATBOT_VIGIA'] = True
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        with patch('app.services.chatbot_vigia._chamar_haiku', return_value=veredicto), \
             patch('app.services.chatbot_vigia._resumo_estoque_loja', return_value=''), \
             patch('app.services.zapi.enviar_texto') as send:
            r = chatbot_vigia.avaliar([{'role': 'user', 'content': 'o que tem na cesta?'}],
                                      conv_id=5)
    assert r['silencio'] is True
    send.assert_not_called()
    assert chatbot_vigia.ultimos()[0]['gravidade'] == 'media'   # fica pro resumo


def test_gerar_link_carrinho():
    from app.services import bot_tools
    r = bot_tools.gerar_link_carrinho([{'sku': '10007', 'qtd': 2}, {'sku': '10009', 'qtd': 1}])
    assert r['link'].endswith('/carrinho?itens=10007:2,10009:1')


def test_gerar_link_carrinho_vazio():
    from app.services import bot_tools
    assert 'erro' in bot_tools.gerar_link_carrinho([])


# ── Fase 4: vigia (IA supervisora) ──

def test_webhook_passa_resposta_do_bot_pro_vigia(app):
    """O historico que vai pro vigia inclui a resposta do bot. Sem isso, o
    vigia nunca consegue julgar o que o bot disse (caso real visto em prod
    em 08/06: vigia avaliando como 'bot ainda nao respondeu')."""
    app.config['CHATBOT_BOT_SECRET'] = app.config['CHATBOT_BOT_SECRET'] = 'seg'
    app.config['CHATBOT_VIGIA'] = True
    app.config['ANTHROPIC_API_KEY'] = 'test'
    app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    client = app.test_client()
    historico_cliente = [{'role': 'user', 'content': 'tem croissant?'}]
    resposta_bot = {'acao': 'responder',
                    'texto': 'Infelizmente está esgotado hoje.'}
    with patch('threading.Thread', _SyncThread), \
         patch('app.services.chatwoot.buscar_historico',
               return_value=historico_cliente), \
         patch('app.services.chatbot.responder', return_value=resposta_bot), \
         patch('app.services.chatwoot.enviar_mensagem', return_value={'ok': True}), \
         patch('app.services.chatwoot.definir_status', return_value={'ok': True}), \
         patch('app.services.chatbot_vigia.disponivel', return_value=True), \
         patch('app.services.chatbot_vigia.avaliar', return_value={}) as vigia:
        _post(client, content='tem croissant?')
    assert vigia.called, 'vigia deveria ter sido chamado'
    historico_passado = vigia.call_args.args[0]
    # Cliente + resposta do bot — vigia tem o contexto completo
    assert len(historico_passado) == 2
    assert historico_passado[0]['role'] == 'user'
    assert historico_passado[1]['role'] == 'assistant'
    assert 'esgotado' in historico_passado[1]['content']


def test_vigia_dispara_alerta_quando_gravidade_alta(app):
    """Vigia avalia, Haiku retorna alerta=alta, envia via Z-API."""
    from app.services import chatbot_vigia
    veredicto = {'alerta': True, 'gravidade': 'alta',
                 'motivo': 'Bot disse esgotado pro croissant que tem na loja',
                 'acao_sugerida': 'Atualizar VNDA'}
    historico = [{'role': 'user', 'content': 'tem croissant?'},
                 {'role': 'assistant', 'content': 'desculpe, esgotado'}]
    with app.app_context():
        app.config['CHATBOT_VIGIA'] = True
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        with patch('app.services.chatbot_vigia._chamar_haiku',
                   return_value=veredicto), \
             patch('app.services.chatbot_vigia._resumo_estoque_loja',
                   return_value='- Croissant: 12 un'), \
             patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as send:
            r = chatbot_vigia.avaliar(historico, conv_id=42, nome_contato='Maria')
    assert r['enviado'] is True
    args = send.call_args
    assert args[0][0] == '5511999990000'   # numero do dono
    msg = args[0][1]
    assert 'ALTA' in msg
    assert 'Maria' in msg
    assert 'croissant' in msg.lower()


def test_vigia_silencia_quando_sem_alerta(app):
    """Conversa normal: vigia decide nao alertar -> Z-API NAO chamado."""
    from app.services import chatbot_vigia
    veredicto = {'alerta': False, 'gravidade': None,
                 'motivo': 'conversa normal', 'acao_sugerida': ''}
    historico = [{'role': 'user', 'content': 'oi'},
                 {'role': 'assistant', 'content': 'olá!'}]
    with app.app_context():
        app.config['CHATBOT_VIGIA'] = True
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        with patch('app.services.chatbot_vigia._chamar_haiku',
                   return_value=veredicto), \
             patch('app.services.chatbot_vigia._resumo_estoque_loja',
                   return_value=''), \
             patch('app.services.zapi.enviar_texto') as send:
            r = chatbot_vigia.avaliar(historico, conv_id=42)
    assert r['silencio'] is True
    send.assert_not_called()


def test_vigia_silencia_quando_gravidade_baixa(app):
    """Anti-spam: gravidade=baixa nao dispara WhatsApp (só log)."""
    from app.services import chatbot_vigia
    veredicto = {'alerta': True, 'gravidade': 'baixa', 'motivo': 'pequeno',
                 'acao_sugerida': ''}
    with app.app_context():
        app.config['CHATBOT_VIGIA'] = True
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        with patch('app.services.chatbot_vigia._chamar_haiku',
                   return_value=veredicto), \
             patch('app.services.chatbot_vigia._resumo_estoque_loja',
                   return_value=''), \
             patch('app.services.zapi.enviar_texto') as send:
            r = chatbot_vigia.avaliar([{'role': 'user', 'content': 'oi'}])
    assert r['silencio'] is True
    send.assert_not_called()


def test_vigia_desligado_pula(app):
    from app.services import chatbot_vigia
    with app.app_context():
        app.config['CHATBOT_VIGIA'] = False
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('app.services.chatbot_vigia._chamar_haiku') as call:
            r = chatbot_vigia.avaliar([{'role': 'user', 'content': 'oi'}])
    assert 'pulou' in r
    call.assert_not_called()


def test_vigia_resumo_estoque_lista_itens_com_saldo(app):
    """Resumo passado pro Haiku traz itens com saldo positivo nas lojas
    (e ele cruza com o que o bot disse pra detectar erro)."""
    from app.extensions import db
    from app.models import EstoqueLoja, Loja, Receita
    from app.services import chatbot_vigia
    with app.app_context():
        loja = Loja(nome='Brooklin', ativa=True)
        receita = Receita(nome='Croissant', categoria='Padaria',
                          rendimento_qtd=1, rendimento_unidade='un',
                          peso_base=80)
        db.session.add_all([loja, receita])
        db.session.flush()
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=receita.id,
                                   quantidade=12))
        db.session.commit()
        resumo = chatbot_vigia._resumo_estoque_loja()
    assert 'Croissant' in resumo
    assert '12' in resumo


def test_vigia_extrai_json_com_markdown_wrapper(app):
    """Haiku as vezes responde ```json {...} ``` — parser tolera."""
    from types import SimpleNamespace

    from app.services import chatbot_vigia
    fake_resp = SimpleNamespace(content=[SimpleNamespace(
        type='text', text='```json\n{"alerta": false, "gravidade": null, '
                          '"motivo": "ok", "acao_sugerida": ""}\n```')])
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic') as M:
            M.return_value.messages.create.return_value = fake_resp
            r = chatbot_vigia._chamar_haiku('test', 'contexto')
    assert r['alerta'] is False


def test_vigia_abandono_alerta_quando_perda_de_venda(app):
    """Cliente pediu produto, bot respondeu, cliente sumiu por 30min — alerta."""
    from app.services import chatbot_vigia
    veredicto = {'alerta': True, 'gravidade': 'alta',
                 'motivo': 'Cliente sumiu sem fechar pedido',
                 'acao_sugerida': 'mandar mensagem proativa'}
    historico = [
        {'role': 'user', 'content': 'Quero a Family Box pra amanhã'},
        {'role': 'assistant', 'content': 'Pra qual endereço?'},
    ]
    with app.app_context():
        chatbot_vigia._historico.clear()
        app.config['CHATBOT_VIGIA'] = True
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        with patch('app.services.chatbot_vigia._chamar_haiku_abandono',
                   return_value=veredicto), \
             patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as send:
            r = chatbot_vigia.avaliar_abandono(
                historico, conv_id=88, nome_contato='Carlos',
                minutos_sem_resposta=30)
    assert r['enviado'] is True
    msg = send.call_args[0][1]
    assert '30 min sem resposta' in msg


def test_vigia_abandono_silencia_quando_conversa_so_cumprimento(app):
    """Cliente disse 'oi', bot respondeu, ninguem voltou — nao deve alertar."""
    from app.services import chatbot_vigia
    veredicto = {'alerta': False, 'gravidade': None,
                 'motivo': 'apenas cumprimento, sem demanda',
                 'acao_sugerida': ''}
    historico = [
        {'role': 'user', 'content': 'oi'},
        {'role': 'assistant', 'content': 'Olá! Como posso ajudar?'},
    ]
    with app.app_context():
        chatbot_vigia._historico.clear()
        app.config['CHATBOT_VIGIA'] = True
        app.config['ANTHROPIC_API_KEY'] = 'test'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511999990000'
        with patch('app.services.chatbot_vigia._chamar_haiku_abandono',
                   return_value=veredicto), \
             patch('app.services.zapi.enviar_texto') as send:
            r = chatbot_vigia.avaliar_abandono(
                historico, conv_id=89, minutos_sem_resposta=20)
    assert r['silencio'] is True
    send.assert_not_called()


def test_chatwoot_listar_conversas_paradas_filtra_por_idade(app):
    """Conversa parada ha 20min entra; conversa de 5min nao."""
    import time as _t

    from app.services import chatwoot
    app.config['CHATWOOT_URL'] = 'https://cw.exemplo.com'
    app.config['CHATWOOT_BOT_TOKEN'] = 'tok'
    app.config['CHATWOOT_ACCOUNT_ID'] = '1'
    agora = _t.time()
    payload = {'data': {'payload': [
        {'id': 11, 'last_activity_at': agora - 20*60,  # 20 min atras
         'meta': {'sender': {'name': 'Maria'}}},
        {'id': 12, 'last_activity_at': agora - 5*60,   # 5 min atras
         'meta': {'sender': {'name': 'Joao'}}},
    ]}}
    fake = SimpleNamespace(status_code=200, text='x', json=lambda: payload)
    with app.app_context():
        with patch('requests.get', return_value=fake):
            paradas = chatwoot.listar_conversas_paradas(min_minutos=15)
    ids = [p['id'] for p in paradas]
    assert 11 in ids
    assert 12 not in ids
    assert paradas[0]['nome_contato'] == 'Maria'


# ── Fase 3: leitura de imagem ──

def test_baixar_imagem_comprime_e_base64(app):
    import base64

    from app.services import chatwoot
    app.config['CHATWOOT_URL'] = 'https://cw.exemplo.com'
    fake = SimpleNamespace(status_code=200, content=b'rawbytes',
                           headers={'Content-Type': 'image/png'})
    with app.app_context():
        with patch('requests.get', return_value=fake), \
             patch('app.utils.comprimir_imagem', return_value=b'jpegbytes'):
            r = chatwoot.baixar_imagem('/rails/blob/123')
    assert r is not None
    media_type, b64 = r
    assert media_type == 'image/jpeg'
    assert base64.b64decode(b64) == b'jpegbytes'


def test_baixar_imagem_formato_nao_suportado(app):
    from app.services import chatwoot
    fake = SimpleNamespace(status_code=200, content=b'heicbytes', headers={})
    with app.app_context():
        with patch('requests.get', return_value=fake), \
             patch('app.utils.comprimir_imagem', side_effect=ValueError('heic')):
            r = chatwoot.baixar_imagem('https://cw/x.heic')
    assert r is None


def test_buscar_historico_extrai_imagem(app):
    from app.services import chatwoot
    app.config['CHATWOOT_URL'] = 'https://cw.exemplo.com'
    app.config['CHATWOOT_BOT_TOKEN'] = 'tok'
    app.config['CHATWOOT_ACCOUNT_ID'] = '1'
    payload = {'payload': [
        {'message_type': 'incoming', 'created_at': 1, 'content': '',
         'attachments': [{'file_type': 'image', 'data_url': 'https://cw/x.png'}]},
    ]}
    fake = SimpleNamespace(status_code=200, text='x', json=lambda: payload)
    with app.app_context():
        with patch('requests.get', return_value=fake):
            hist = chatwoot.buscar_historico(7)
    assert hist[-1]['imagens'] == ['https://cw/x.png']
    assert hist[-1]['role'] == 'user'


def test_build_messages_imagem_so_na_ultima(app):
    from app.services import chatbot
    historico = [
        {'role': 'user', 'content': 'oi', 'imagens': ['https://cw/velha.png']},
        {'role': 'assistant', 'content': 'olá'},
        {'role': 'user', 'content': 'olha esse pedido',
         'imagens': ['https://cw/atual.png']},
    ]
    with app.app_context():
        with patch('app.services.chatwoot.baixar_imagem',
                   return_value=('image/jpeg', 'BASE64')) as bi:
            msgs = chatbot._build_messages(historico)
    # so a ultima mensagem do cliente vira blocos com imagem
    ult = msgs[-1]
    assert isinstance(ult['content'], list)
    tipos = [b['type'] for b in ult['content']]
    assert 'image' in tipos and 'text' in tipos
    img_block = next(b for b in ult['content'] if b['type'] == 'image')
    assert img_block['source']['data'] == 'BASE64'
    # a 1a mensagem (imagem antiga) fica só texto — baixa 1 imagem só
    assert isinstance(msgs[0]['content'], str)
    bi.assert_called_once_with('https://cw/atual.png')


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


def test_consultar_produtos_variants_lista_de_id(app):
    """Formato REAL do VNDA: variants = lista de {id: variante}. O parser tem
    que mergulhar no wrapper e pegar o sku — senão volta catálogo vazio."""
    from app.services import bot_tools
    bot_tools._catalogo_cache.clear()
    fake = SimpleNamespace(json=lambda: {'products': [
        {'id': 59, 'name': 'Lancheira Especial', 'available': True,
         'variants': [{'61': {'id': 61, 'sku': '10054', 'sale_price': 57.0,
                              'price': 57.0, 'available': True, 'name': ''}}]}]})
    with app.app_context():
        with patch('app.services.vnda._get', return_value=fake):
            r = bot_tools.consultar_produtos('lancheira')
    assert 'erro' not in r
    assert r['produtos'], 'catálogo não pode vir vazio'
    p = r['produtos'][0]
    assert p['sku'] == '10054'   # extraído de dentro do wrapper {id: variante}
    assert p['preco'] == 57.0    # sale_price


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

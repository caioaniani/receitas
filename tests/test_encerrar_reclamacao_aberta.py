"""Item 7 (dono, caso E3862E49, 22-23/09/2026): o bot NÃO encerra conversa
com reclamação em aberto.

Encerrar automaticamente (resolved sem fala) só quando o último turno do
cliente é fechamento ("obrigada", "resolvido") E não há reclamação/falha
registrada sem resposta HUMANA depois. Caso contrário a conversa vai para
`open` e fica na fila da equipe — nunca `resolved`. Vale para a Camada 1,
para a tool `encerrar_conversa` do modelo, para o turno vazio e, por eles,
para o webhook e a vassoura. Anthropic/Chatwoot mockados.
"""
from types import SimpleNamespace
from unittest.mock import patch


def test_pode_encerrar_regras():
    from app.services.chatbot import pode_encerrar
    # fechamento puro sem reclamação → pode
    assert pode_encerrar([{'role': 'assistant', 'content': 'Link enviado!'},
                          {'role': 'user', 'content': 'obrigada'}]) is True
    # último turno não é fechamento → não
    assert pode_encerrar([{'role': 'user', 'content': 'quero uma cesta'}]) is False
    # reclamação sem resposta humana + "obrigada" → não
    hist = [{'role': 'user', 'content': 'não recebi meu pedido'},
            {'role': 'assistant', 'content': 'Sinto muito! Já estou passando pra equipe.'},
            {'role': 'user', 'content': 'obrigada'}]
    assert pode_encerrar(hist) is False
    # a mesma conversa com resposta HUMANA depois da reclamação → pode
    hist_h = hist[:2] + [{'role': 'assistant', 'humano': True,
                          'content': 'Oi! Reenviamos agora, chega em 40 min.'},
                         {'role': 'user', 'content': 'obrigada'}]
    assert pode_encerrar(hist_h) is True
    # fechamento que o `_e_fechamento` NAO reconhece ("Obrigada. Esclareceu")
    # so passa com exigir_fechamento=False — e mesmo assim a reclamacao
    # em aberto barra.
    hist_e = hist[:2] + [{'role': 'user', 'content': 'Obrigada. Esclareceu'}]
    assert pode_encerrar(hist_e) is False
    assert pode_encerrar(hist_e, exigir_fechamento=False) is False
    assert pode_encerrar([{'role': 'assistant', 'content': 'Link enviado!'},
                          {'role': 'user', 'content': 'Obrigada. Esclareceu'}],
                         exigir_fechamento=False) is True
    # reclamação HERDADA de conversa anterior não conta
    hist_herd = [{'role': 'user', 'content': 'não recebi meu pedido', 'herdada': True},
                 {'role': 'assistant', 'content': 'Já passei pra equipe', 'herdada': True},
                 {'role': 'assistant', 'content': 'Aqui está o link!'},
                 {'role': 'user', 'content': 'valeu'}]
    assert pode_encerrar(hist_herd) is True


def test_camada_1_obrigada_com_reclamacao_vai_pra_fila_sem_resolver(app):
    from app.services import chatbot
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic') as M:
            r = chatbot.responder([
                {'role': 'user', 'content': 'veio faltando o pão de queijo'},
                {'role': 'assistant', 'content': 'Sinto muito! Já estou passando pra equipe.'},
                {'role': 'user', 'content': 'obrigada'}])
        M.return_value.messages.create.assert_not_called()
    assert r['acao'] == 'handoff' and r['texto'] == ''      # fila, em silêncio
    assert 'reclamação em aberto' in r['motivo']
    assert r['tools_usadas'] == []


def test_camada_1_obrigada_sem_reclamacao_segue_encerrando(app):
    from app.services import chatbot
    with app.app_context():
        app.config['ANTHROPIC_API_KEY'] = 'test'
        with patch('anthropic.Anthropic'):
            r = chatbot.responder([
                {'role': 'assistant', 'content': 'Aqui está o link do carrinho!'},
                {'role': 'user', 'content': 'Muito obrigada🙏'}])
    assert r['acao'] == 'encerrar'


def test_tool_encerrar_do_modelo_e_recusada_com_reclamacao_aberta(app, monkeypatch):
    from app.services import chatbot
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-x')
    blk = SimpleNamespace(type='tool_use', name='encerrar_conversa', id='t1', input={})

    class FakeClient:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(
                create=lambda **kw: SimpleNamespace(content=[blk], stop_reason='tool_use'))
    monkeypatch.setattr('anthropic.Anthropic', FakeClient)
    with app.app_context():
        r = chatbot.responder([
            {'role': 'user', 'content': 'o motoboy foi embora e não entregou'},
            {'role': 'assistant', 'content': 'Sinto muito! Passei pra equipe. Posso ajudar em mais algo?'},
            {'role': 'user', 'content': 'ok'}])
    assert r['acao'] == 'handoff' and r['texto'] == ''
    assert 'encerrar_conversa' in r['tools_usadas']


def test_webhook_fila_silenciosa_vai_para_open_sem_falar(app):
    """No webhook: handoff sem texto = nenhuma mensagem ao cliente, status
    `open`, nunca `resolved`."""
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    app.config['CHATWOOT_URL'] = 'https://atendimento.x.com'
    app.config['CHATWOOT_ACCOUNT_ID'] = '1'
    app.config['CHATWOOT_BOT_TOKEN'] = 'bot-tok'
    c = app.test_client()
    payload = {'event': 'message_created', 'id': 7001, 'message_type': 'incoming',
               'content': 'obrigada', 'conversation': {'id': 778, 'status': 'pending'},
               'sender': {'name': 'Cliente'}}

    class _SyncThread:
        def __init__(self, target=None, daemon=None, **kw):
            self._target = target

        def start(self):
            if self._target:
                self._target()

    with patch('threading.Thread', _SyncThread), \
            patch('app.services.chatbot.responder',
                  return_value={'acao': 'handoff', 'texto': '',
                                'motivo': 'reclamação em aberto sem resposta humana'}), \
            patch('app.services.chatbot.carregar_historico', return_value=None), \
            patch('app.services.chatwoot.buscar_historico', return_value=[]), \
            patch('app.services.chatbot.salvar_historico'), \
            patch('app.services.entrega_candidata.anotar_handoff', return_value={'ok': True}), \
            patch('app.services.chatwoot.enviar_mensagem') as enviar, \
            patch('app.services.chatwoot.definir_status', return_value={'ok': True}) as status, \
            patch('app.services.chatbot_vigia.disponivel', return_value=False):
        r = c.post('/crm/bot?k=seg', json=payload)
    assert r.status_code == 200
    enviar.assert_not_called()
    status.assert_called_with(778, 'open')

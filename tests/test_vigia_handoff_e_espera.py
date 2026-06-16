"""Vigia — detectores B (handoff preguicoso) e C (cliente esperando humano).

Ambos nasceram do incidente de 12/06/2026 (conv #198, Mariana):
- B: o bot transferiu pro humano sem nem consultar o catalogo quando a
  cliente perguntou 'tem cesta? entrega amanha?'. Vigia agora recebe a
  lista de FERRAMENTAS USADAS e classifica handoff-sem-ferramenta +
  cliente-comprando como ALTA (alerta na hora).
- C: depois do handoff, a cliente mandou 'Ola' numa conversa `open` e
  ninguem viu — o bot ignora open, o detector de abandono so olha
  pending. Detector novo, deterministico, alerta o dono.
"""
from unittest.mock import patch

# ── B: handoff preguicoso vira sinal pro Haiku ──────────────────────────


def test_vigia_recebe_tools_usadas_no_contexto(app):
    """O resultado do bot carrega tools_usadas; o vigia injeta isso no
    prompt do Haiku como 'FERRAMENTAS USADAS'.

    Nota: usa `acao=responder` pra evitar o detector deterministico
    (handoff preguicoso em venda, 16/06/2026) que pularia o Haiku.
    O ponto do teste é garantir que QUANDO o Haiku é chamado, ele
    recebe a info de FERRAMENTAS USADAS."""
    from app.services import chatbot_vigia
    capturado = {}

    def fake_haiku(api_key, contexto):
        capturado['ctx'] = contexto
        return {'alerta': True, 'gravidade': 'alta',
                'motivo': 'handoff sem consultar catalogo',
                'acao_sugerida': 'retomar cliente'}

    with app.app_context(), \
         patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'x'}), \
         patch.object(app.config, 'get', wraps=app.config.get), \
         patch('app.services.chatbot_vigia._chamar_haiku',
               side_effect=fake_haiku), \
         patch('app.services.chatbot_vigia._resumo_catalogo_site',
               return_value='- Family Box: DISPONIVEL'), \
         patch('app.services.zapi.enviar_texto',
               return_value={'ok': True}):
        app.config['CHATBOT_VIGIA'] = '1'
        chatbot_vigia._avaliar_interno(
            [{'role': 'user', 'content': 'tem cesta? entrega amanha?'}],
            conv_id=198, nome_contato='Mariana',
            resultado_bot={'acao': 'responder', 'motivo': '',
                           'tools_usadas': []})
    assert 'FERRAMENTAS USADAS' in capturado['ctx']
    assert 'NENHUMA' in capturado['ctx']


def test_vigia_mostra_tools_quando_bot_consultou(app):
    from app.services import chatbot_vigia
    capturado = {}

    def fake_haiku(api_key, contexto):
        capturado['ctx'] = contexto
        return {'alerta': False, 'gravidade': None, 'motivo': 'ok'}

    with app.app_context(), \
         patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'x'}), \
         patch('app.services.chatbot_vigia._chamar_haiku',
               side_effect=fake_haiku), \
         patch('app.services.chatbot_vigia._resumo_catalogo_site',
               return_value=''):
        app.config['CHATBOT_VIGIA'] = '1'
        chatbot_vigia._avaliar_interno(
            [{'role': 'user', 'content': 'quero a family box'}],
            conv_id=1, nome_contato='Ana',
            resultado_bot={'acao': 'responder',
                           'tools_usadas': ['consultar_produtos']})
    assert 'consultar_produtos' in capturado['ctx']


def test_prompt_vigia_tem_regra_handoff_preguicoso(app):
    import pathlib
    src = pathlib.Path('app/services/chatbot_vigia.py').read_text()
    assert 'HANDOFF PREGUIÇOSO EM VENDA' in src
    assert 'HANDOFF PREGUIÇOSO EM CONSULTA DE PEDIDO' in src
    assert 'FERRAMENTAS USADAS' in src
    # exemplo concreto do caso #198
    assert 'entrega' in src.lower() and 'amanhã' in src
    # regressao do falso positivo (12/06/2026 noite):
    # reclamacao de entrega NAO e handoff preguicoso
    assert 'não chegou' in src
    assert 'reclamação' in src.lower() or 'reclamacao' in src.lower()


def test_chatbot_responder_inclui_tools_usadas(app):
    """O bot expoe tools_usadas em TODOS os caminhos de retorno — o
    vigia depende disso pra detectar handoff preguicoso."""
    import pathlib
    src = pathlib.Path('app/services/chatbot.py').read_text()
    # Conta returns de acao e returns com tools_usadas — todo dict de
    # resultado do responder() precisa carregar tools_usadas
    import re
    # Os returns dentro de responder() (apos a definicao de tools_usadas)
    trecho = src[src.index('tools_usadas = []'):src.index(
        '# ── Follow-up')]
    acoes = len(re.findall(r"'acao':", trecho))
    com_tools = trecho.count("'tools_usadas': tools_usadas")
    assert com_tools >= acoes, (
        f'{acoes} returns com acao mas so {com_tools} com tools_usadas — '
        'algum caminho de retorno esqueceu tools_usadas')


# ── C: cliente esperando humano em conversa `open` ──────────────────────


def test_alerta_cliente_esperando_humano(app):
    from app.models import VigiaVeredito
    from app.services import chatbot_vigia
    with app.app_context(), \
         patch('app.services.chatbot_vigia._numero_destino',
               return_value='5511999990000'), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=[{'id': 198, 'nome_contato': 'Mariana',
                              'minutos_paradas': 25}]), \
         patch('app.services.chatwoot.buscar_historico',
               return_value=[{'role': 'assistant', 'content': 'um atendente vai te ajudar'},
                             {'role': 'user', 'content': 'Olá'}]), \
         patch('app.services.zapi.enviar_texto',
               return_value={'ok': True}) as envia:
        r = chatbot_vigia.alertar_clientes_esperando_humano()
        assert r == {'avaliadas': 1, 'enviadas': 1}
        envia.assert_called_once()
        msg = envia.call_args[0][1]
        assert 'esperando' in msg.lower()
        assert 'Mariana' in msg
        # Registrou e nao re-alerta
        row = VigiaVeredito.query.filter(
            VigiaVeredito.conv_id == '198',
            VigiaVeredito.mensagem_cliente.like('[ESPERA_HUMANO%')).first()
        assert row is not None
        r2 = chatbot_vigia.alertar_clientes_esperando_humano()
        assert r2['enviadas'] == 0


def test_nao_alerta_se_ultima_msg_e_do_atendente(app):
    """Atendente respondeu por ultimo = atendimento em andamento, nao
    alertar (senao incomoda o dono no meio de um atendimento normal)."""
    from app.services import chatbot_vigia
    with app.app_context(), \
         patch('app.services.chatbot_vigia._numero_destino',
               return_value='5511999990000'), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=[{'id': 50, 'minutos_paradas': 30}]), \
         patch('app.services.chatwoot.buscar_historico',
               return_value=[{'role': 'user', 'content': 'oi'},
                             {'role': 'assistant', 'content': 'oi! tudo bem?'}]), \
         patch('app.services.zapi.enviar_texto') as envia:
        r = chatbot_vigia.alertar_clientes_esperando_humano()
    assert r['enviadas'] == 0
    envia.assert_not_called()


def test_espera_humano_ignora_conversa_fria(app):
    from app.services import chatbot_vigia
    with app.app_context(), \
         patch('app.services.chatbot_vigia._numero_destino',
               return_value='5511999990000'), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=[{'id': 60, 'minutos_paradas': 5000}]), \
         patch('app.services.zapi.enviar_texto') as envia:
        r = chatbot_vigia.alertar_clientes_esperando_humano(max_minutos=720)
    assert r['enviadas'] == 0
    envia.assert_not_called()


def test_espera_humano_sem_numero_pula(app):
    from app.services import chatbot_vigia
    with app.app_context(), \
         patch('app.services.chatbot_vigia._numero_destino',
               return_value=''):
        r = chatbot_vigia.alertar_clientes_esperando_humano()
    assert r == {'pulou': 'sem numero destino'}


def test_listar_conversas_paradas_aceita_status_open(app):
    """A funcao agora aceita status=open (alem do pending default) — e o
    que destrava o detector C."""
    import inspect

    from app.services import chatwoot
    sig = inspect.signature(chatwoot.listar_conversas_paradas)
    assert 'status' in sig.parameters
    assert sig.parameters['status'].default == 'pending'

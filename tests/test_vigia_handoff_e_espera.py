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
         patch('app.services.chatbot_vigia._chamar_modelo',
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
         patch('app.services.chatbot_vigia._chamar_modelo',
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


# ── Menção de story do IG não é cliente esperando (06/07/2026) ──────────

def test_mencao_story_nao_dispara_espera_humano(app):
    """Caso Camila (conversa 566): 'camilacasquel mentioned you in the
    story:' disparou o alerta de cliente esperando atendente — mas menção
    de story é marcação social, ninguém está esperando (decisão do dono)."""
    from unittest.mock import patch

    from app.services import chatbot_vigia
    with app.app_context():
        paradas = [{'id': 566, 'minutos_paradas': 14,
                    'nome_contato': 'Camila'}]
        historico = [{'role': 'user',
                      'content': 'camilacasquel mentioned you in the story:'}]
        with patch('app.services.chatbot_vigia._numero_destino',
                   return_value='5511999999999'), \
                patch('app.services.chatwoot.listar_conversas_paradas',
                      return_value=paradas), \
                patch('app.services.chatwoot.buscar_historico',
                      return_value=historico), \
                patch('app.services.zapi.enviar_texto') as tx:
            chatbot_vigia.alertar_clientes_esperando_humano()
        tx.assert_not_called()


def test_mencao_story_nao_gasta_modelo_no_abandono(app):
    from unittest.mock import patch

    from app.services import chatbot_vigia
    with app.app_context(), \
            patch('app.services.chatbot_vigia.disponivel', return_value=True):
        historico = [{'role': 'user',
                      'content': 'fulana mentioned you in the story:'}]
        r = chatbot_vigia.avaliar_abandono(historico, conv_id=566,
                                           nome_contato='Camila',
                                           minutos_sem_resposta=30)
        assert r == {'pulou': 'mencao de story do Instagram'}


def test_dm_real_do_instagram_continua_alertando(app):
    """Cliente de verdade no IG DM (com texto normal) segue disparando o
    alerta de espera — o skip é só pra menção de story."""
    from unittest.mock import patch

    from app.services import chatbot_vigia
    with app.app_context():
        app.config['CHATWOOT_URL'] = 'https://x.example'
        app.config['CHATWOOT_ACCOUNT_ID'] = '1'
        paradas = [{'id': 567, 'minutos_paradas': 14,
                    'nome_contato': 'Cliente Real'}]
        historico = [{'role': 'user',
                      'content': 'oi, meu pedido chega que horas?'}]
        with patch('app.services.chatbot_vigia._numero_destino',
                   return_value='5511999999999'), \
                patch('app.services.chatwoot.listar_conversas_paradas',
                      return_value=paradas), \
                patch('app.services.chatwoot.buscar_historico',
                      return_value=historico), \
                patch.object(chatbot_vigia, '_ja_avisado_espera_humano',
                             return_value=False), \
                patch('app.services.zapi.enviar_texto',
                      return_value={'ok': True}) as tx:
            chatbot_vigia.alertar_clientes_esperando_humano()
        assert tx.called
        assert 'esperando ATENDENTE' in tx.call_args[0][1]


def test_espera_humano_manda_contencao_ao_cliente(app):
    """Dono 09/08/2026 (12 clientes no vácuo 10-14min no Dia dos Pais):
    junto do alerta ao dono, o CLIENTE recebe 1 mensagem de contenção na
    conversa ("a equipe já te responde"). Dedupe herdado do alerta;
    ESPERA_HUMANO_CONTENCAO=0 desliga."""
    import os

    from app.services import chatbot_vigia
    base = {'id': 320, 'nome_contato': 'Bia', 'minutos_paradas': 15}
    hist = [{'role': 'user', 'content': 'Vocês têm cesta de café?'}]
    with app.app_context(), \
         patch('app.services.chatbot_vigia._numero_destino',
               return_value='5511999990000'), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=[base]), \
         patch('app.services.chatwoot.buscar_historico', return_value=hist), \
         patch('app.services.chatwoot.enviar_mensagem',
               return_value={'ok': True}) as contem, \
         patch('app.services.zapi.enviar_texto', return_value={'ok': True}):
        chatbot_vigia.alertar_clientes_esperando_humano()
    contem.assert_called_once()
    assert contem.call_args[0][0] == 320
    assert 'já vai te responder' in contem.call_args[0][1]

    # Kill-switch desliga só a contenção (alerta ao dono segue).
    base2 = {'id': 321, 'nome_contato': 'Cau', 'minutos_paradas': 15}
    os.environ['ESPERA_HUMANO_CONTENCAO'] = '0'
    try:
        with app.app_context(), \
             patch('app.services.chatbot_vigia._numero_destino',
                   return_value='5511999990000'), \
             patch('app.services.chatwoot.listar_conversas_paradas',
                   return_value=[base2]), \
             patch('app.services.chatwoot.buscar_historico',
                   return_value=hist), \
             patch('app.services.chatwoot.enviar_mensagem') as contem2, \
             patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as alerta:
            chatbot_vigia.alertar_clientes_esperando_humano()
        contem2.assert_not_called()
        alerta.assert_called_once()
    finally:
        os.environ.pop('ESPERA_HUMANO_CONTENCAO', None)


def test_contencao_nao_duplica_pro_mesmo_contato_em_duas_conversas(app):
    """Caso Lissa (19/08/2026): a MESMA cliente em DUAS conversas do
    Chatwoot (IG junta tudo numa thread) recebia a contenção em cada uma.
    O alerta ao dono segue POR CONVERSA (ele quer saber das duas), mas a
    contenção ao cliente sai UMA vez por CONTATO em 12h."""
    from app.services import chatbot_vigia
    paradas = [
        {'id': 1723, 'nome_contato': 'Lissa', 'minutos_paradas': 13,
         'telefone': '17841400000001'},
        {'id': 1730, 'nome_contato': 'Lissa', 'minutos_paradas': 14,
         'telefone': '17841400000001'},
    ]
    hist = [{'role': 'user', 'content': 'Av omega 219 apt 144'}]
    with app.app_context(), \
         patch('app.services.chatbot_vigia._numero_destino',
               return_value='5511999990000'), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=paradas), \
         patch('app.services.chatwoot.buscar_historico', return_value=hist), \
         patch('app.services.chatwoot.enviar_mensagem',
               return_value={'ok': True}) as contem, \
         patch('app.services.zapi.enviar_texto',
               return_value={'ok': True}) as alerta:
        chatbot_vigia.alertar_clientes_esperando_humano()
    assert alerta.call_count == 2          # dono sabe das DUAS conversas
    contem.assert_called_once()            # cliente recebe UMA contenção
    assert contem.call_args[0][0] == 1723


def test_contencao_nao_repete_se_ja_esta_na_conversa(app):
    """Re-alerta pós-12h (dedupe da conversa expira) não re-manda o MESMO
    texto enlatado pro cliente: se a contenção já está no histórico da
    conversa, só o alerta ao dono sai."""
    from app.services import chatbot_vigia
    paradas = [{'id': 900, 'nome_contato': 'Rê', 'minutos_paradas': 20,
                'telefone': '5511977776666'}]
    hist = [
        {'role': 'user', 'content': 'alguém me responde?'},
        {'role': 'assistant',
         'content': chatbot_vigia.TEXTO_CONTENCAO_ESPERA},
        {'role': 'user', 'content': 'sigo aguardando'},
    ]
    with app.app_context(), \
         patch('app.services.chatbot_vigia._numero_destino',
               return_value='5511999990000'), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=paradas), \
         patch('app.services.chatwoot.buscar_historico', return_value=hist), \
         patch('app.services.chatwoot.enviar_mensagem') as contem, \
         patch('app.services.zapi.enviar_texto',
               return_value={'ok': True}) as alerta:
        chatbot_vigia.alertar_clientes_esperando_humano()
    alerta.assert_called_once()
    contem.assert_not_called()


# ── Anti-duplicata: claim ANTES do envio (20/08/2026) ───────────────────
# Caso do dono ("duplo texto do bot, muito serio"): a MESMA mensagem duas
# vezes no grupo. A ordem antiga (envia -> registra) deixava dois processos
# no mesmo ciclo (2 workers gunicorn, ou container velho + novo no deploy)
# passarem juntos pelo dedupe. O registro E o dedupe: tem que estar
# COMMITADO quando o WhatsApp sai.

def _conversa_parada(conv_id=1759, minutos=12):
    return [{'id': conv_id, 'nome_contato': 'Dany', 'minutos_paradas': minutos}]


_HIST_ESPERANDO = [{'role': 'assistant', 'content': 'ja te respondo'},
                   {'role': 'user', 'content': 'Boa tarde'}]


def test_claim_esta_gravado_ANTES_de_enviar(app):
    """O mock de envio consulta o banco NO MOMENTO do disparo: se o
    registro ainda nao estiver la, um segundo processo passaria pelo
    dedupe e mandaria de novo."""
    from app.models import VigiaVeredito
    from app.services import chatbot_vigia
    visto = {}

    def _envia(numero, msg):
        visto['ja_registrado'] = VigiaVeredito.query.filter(
            VigiaVeredito.conv_id == '1759',
            VigiaVeredito.mensagem_cliente.like('[ESPERA_HUMANO%')
        ).first() is not None
        return {'ok': True}

    with app.app_context(), \
         patch('app.services.chatbot_vigia._numero_destino',
               return_value='5511999990000'), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=_conversa_parada()), \
         patch('app.services.chatwoot.buscar_historico',
               return_value=_HIST_ESPERANDO), \
         patch('app.services.chatwoot.enviar_mensagem',
               return_value={'ok': True}), \
         patch('app.services.zapi.enviar_texto', side_effect=_envia):
        r = chatbot_vigia.alertar_clientes_esperando_humano()
    assert r['enviadas'] == 1
    assert visto['ja_registrado'] is True


def test_envio_falho_desfaz_o_claim_e_retenta(app):
    """Z-API fora nao pode virar dedupe de 12h: o cliente ficaria esperando
    sem que ninguem fosse avisado."""
    from app.models import VigiaVeredito
    from app.services import chatbot_vigia
    with app.app_context(), \
         patch('app.services.chatbot_vigia._numero_destino',
               return_value='5511999990000'), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=_conversa_parada()), \
         patch('app.services.chatwoot.buscar_historico',
               return_value=_HIST_ESPERANDO), \
         patch('app.services.chatwoot.enviar_mensagem',
               return_value={'ok': True}), \
         patch('app.services.zapi.enviar_texto',
               return_value={'ok': False, 'erro': 'z-api fora'}):
        r1 = chatbot_vigia.alertar_clientes_esperando_humano()
        assert r1['enviadas'] == 0
        assert VigiaVeredito.query.filter(
            VigiaVeredito.conv_id == '1759',
            VigiaVeredito.mensagem_cliente.like('[ESPERA_HUMANO%')
        ).first() is None                      # claim devolvido
        # proximo ciclo: com a Z-API de volta, o alerta sai
        with patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as envia:
            r2 = chatbot_vigia.alertar_clientes_esperando_humano()
        assert r2['enviadas'] == 1 and envia.called


def test_segundo_processo_no_mesmo_ciclo_nao_duplica(app):
    """Simula os 2 workers: a segunda passada (com o claim ja no banco) nao
    manda de novo — era exatamente isso que chegava dobrado no WhatsApp."""
    from app.services import chatbot_vigia
    with app.app_context(), \
         patch('app.services.chatbot_vigia._numero_destino',
               return_value='5511999990000'), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=_conversa_parada()), \
         patch('app.services.chatwoot.buscar_historico',
               return_value=_HIST_ESPERANDO), \
         patch('app.services.chatwoot.enviar_mensagem',
               return_value={'ok': True}), \
         patch('app.services.zapi.enviar_texto',
               return_value={'ok': True}) as envia:
        chatbot_vigia.alertar_clientes_esperando_humano()   # worker A
        chatbot_vigia.alertar_clientes_esperando_humano()   # worker B
    assert envia.call_count == 1

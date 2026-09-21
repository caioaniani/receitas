"""Nota privada da equipe cala o bot (dono 20/09/2026, caso conv 2409).

"O bot não pode falar quando a gente fala no privado com a equipe." Às
19:06 o dono reabriu a conversa do entregador e escreveu a nota privada
"@Painel"; às 19:20 o sistema mandou ao contato o texto automático de
contenção da espera humana. A nota privada de agente HUMANO vira o
marcador `PresencaHumanaConversa`; toda fala automática ao contato
(resposta do bot, resposta a áudio, follow-up, vassoura, contenção)
consulta `presenca_humana.humano_presente` e fica em silêncio. A
COBRANÇA ao dono continua até resolver — só o texto ao contato é barrado.
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from app.utils import agora


class _SyncThread:
    def __init__(self, target=None, daemon=None, **kw):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def _nota(conv_status='pending', sender_type='user', **over):
    payload = {
        'event': 'message_created', 'id': 9001, 'message_type': 'outgoing',
        'private': True, 'content': '@Painel liga pra ele',
        'conversation': {'id': 7, 'status': conv_status},
        'sender': {'id': 3, 'name': 'Caio Antinhani', 'type': sender_type},
    }
    payload.update(over)
    return payload


def _incoming(**over):
    payload = {'event': 'message_created', 'id': 9100, 'message_type': 'incoming',
               'conversation': {'id': 7, 'status': 'pending'}, 'content': 'oi',
               'sender': {'name': 'Ale', 'phone_number': '+5511910935006'}}
    payload.update(over)
    return payload


def _marcar(conv_id='7', horas_atras=0, autor='Caio'):
    from app.services import presenca_humana
    return presenca_humana.registrar_nota_privada(
        conv_id, autor, quando=agora() - timedelta(hours=horas_atras))


# ── 1. Webhook: a nota privada humana grava o marcador ──

def test_nota_privada_humana_grava_marcador_e_tira_do_bot(app):
    from app.models import PresencaHumanaConversa
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    c = app.test_client()
    with app.app_context(), patch('threading.Thread', _SyncThread), \
         patch('app.services.chatwoot.definir_status',
               return_value={'ok': True}) as st, \
         patch('app.services.chatbot.responder') as resp:
        r = c.post('/crm/bot?k=seg', json=_nota())
        body = r.get_json()
        row = app.extensions['sqlalchemy'].session.get(PresencaHumanaConversa, '7')
        assert row is not None
        assert row.autor == 'Caio Antinhani'
        assert row.notas == 1
        assert row.aberta_em is not None   # saiu do bot por causa da nota
    assert r.status_code == 200
    assert body['nota_humana'] is True and body['aberta'] is True
    st.assert_called_once_with(7, 'open')
    resp.assert_not_called()


def test_nota_privada_em_conversa_open_so_marca(app):
    """Conversa já com humano (open): marca, mas não mexe no status."""
    from app.models import PresencaHumanaConversa
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    c = app.test_client()
    with app.app_context(), patch('threading.Thread', _SyncThread), \
         patch('app.services.chatwoot.definir_status') as st:
        r = c.post('/crm/bot?k=seg', json=_nota(conv_status='open'))
        body = r.get_json()
        row = app.extensions['sqlalchemy'].session.get(PresencaHumanaConversa, '7')
        assert row is not None and row.aberta_em is None
    assert body['nota_humana'] is True and body['aberta'] is False
    st.assert_not_called()


def test_segunda_nota_atualiza_o_marcador(app):
    from app.models import PresencaHumanaConversa
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    c = app.test_client()
    with app.app_context(), patch('threading.Thread', _SyncThread), \
         patch('app.services.chatwoot.definir_status', return_value={'ok': True}):
        _marcar(horas_atras=5, autor='Ana')
        c.post('/crm/bot?k=seg', json=_nota(conv_status='open'))
        row = app.extensions['sqlalchemy'].session.get(PresencaHumanaConversa, '7')
        assert row.notas == 2
        assert row.autor == 'Caio Antinhani'
        assert (agora() - row.nota_em).total_seconds() < 60


def test_nota_do_bot_ou_de_automacao_nao_conta(app):
    """Só HUMANO cala o bot: agent_bot, contato e automação não marcam."""
    from app.models import PresencaHumanaConversa
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    c = app.test_client()
    casos = [_nota(sender_type='agent_bot'),
             _nota(sender_type='contact'),
             _nota(content_attributes={'automation_rule_id': 4}),
             _nota(sender=None, sender_type='AgentBot')]
    with app.app_context(), patch('threading.Thread', _SyncThread), \
         patch('app.services.chatwoot.definir_status') as st:
        for p in casos:
            r = c.post('/crm/bot?k=seg', json=p)
            assert r.get_json()['ignorado'] == 'nota-nao-humana', p
        assert app.extensions['sqlalchemy'].session.get(
            PresencaHumanaConversa, '7') is None
    st.assert_not_called()


def test_mensagem_publica_outgoing_segue_ignorada_como_antes(app):
    """A nota privada passou a ser tratada ANTES do filtro de incoming;
    mensagem pública do atendente continua 'nao-incoming' (anti-loop)."""
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    c = app.test_client()
    r = c.post('/crm/bot?k=seg', json=_nota(private=False))
    assert r.get_json()['ignorado'] == 'nao-incoming'


# ── 2. Bot em silêncio enquanto a equipe fala no privado ──

def test_bot_nao_responde_com_humano_presente(app):
    from app.services import chatbot
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    c = app.test_client()
    with app.app_context():
        _marcar()
    with patch('threading.Thread', _SyncThread), \
         patch('app.services.chatwoot.buscar_historico', return_value=[]), \
         patch('app.services.chatbot.responder') as resp, \
         patch('app.services.chatwoot.enviar_mensagem') as env, \
         patch('app.services.chatwoot.definir_status',
               return_value={'ok': True}) as st, \
         patch('app.services.chatbot_vigia.avaliar') as vigia:
        r = c.post('/crm/bot?k=seg', json=_incoming(content='e aí, alguém?'))
    assert r.status_code == 200
    resp.assert_not_called()          # nem chama o modelo
    env.assert_not_called()           # nenhuma fala ao contato
    st.assert_called_once_with(7, 'open')   # fila humana garantida
    vigia.assert_not_called()         # não há turno do bot pra julgar
    with app.app_context():
        store = chatbot.carregar_historico('7')
    assert store and store[-1]['role'] == 'user'
    assert store[-1]['content'] == 'e aí, alguém?'   # contexto preservado


def test_marcador_velho_nao_cala_o_bot(app):
    """Depois de PRESENCA_HUMANA_HORAS a conversa volta ao bot (conversa
    resolvida e reaberta dias depois pelo mesmo contato)."""
    from app.services import presenca_humana
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    c = app.test_client()
    with app.app_context():
        _marcar(horas_atras=presenca_humana.PRESENCA_HUMANA_HORAS + 1)
        assert not presenca_humana.humano_presente('7')
    with patch('threading.Thread', _SyncThread), \
         patch('app.services.chatwoot.buscar_historico', return_value=[]), \
         patch('app.services.chatbot.responder',
               return_value={'acao': 'responder', 'texto': 'Oi!'}) as resp, \
         patch('app.services.chatwoot.enviar_mensagem',
               return_value={'ok': True}) as env, \
         patch('app.services.chatwoot.definir_status') as st:
        c.post('/crm/bot?k=seg', json=_incoming())
    resp.assert_called_once()
    env.assert_called_once()
    st.assert_not_called()


def test_audio_com_humano_presente_nao_pede_texto(app):
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    c = app.test_client()
    with app.app_context():
        _marcar()
    payload = _incoming(content='', attachments=[{'file_type': 'audio',
                                                  'data_url': 'https://x/a.ogg'}])
    with patch('threading.Thread', _SyncThread), \
         patch('app.services.chatwoot.enviar_mensagem') as env, \
         patch('app.services.chatwoot.definir_status',
               return_value={'ok': True}) as st:
        r = c.post('/crm/bot?k=seg', json=payload)
    assert r.get_json()['acao'] == 'anexo-nao-suportado'
    env.assert_not_called()
    st.assert_called_once_with(7, 'open')


def test_audio_sem_humano_segue_pedindo_texto(app):
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    c = app.test_client()
    payload = _incoming(content='', attachments=[{'file_type': 'audio',
                                                  'data_url': 'https://x/a.ogg'}])
    with patch('threading.Thread', _SyncThread), \
         patch('app.services.chatwoot.enviar_mensagem',
               return_value={'ok': True}) as env, \
         patch('app.services.chatwoot.definir_status') as st:
        c.post('/crm/bot?k=seg', json=payload)
    env.assert_called_once()
    assert 'Pode me escrever' in env.call_args[0][1]
    st.assert_not_called()


# ── 3. Follow-up e vassoura ──

def test_followup_pula_conversa_com_humano_presente(app):
    from app.services import chatbot
    paradas = [{'id': 7, 'minutos_paradas': 8, 'nome_contato': 'Ale'}]
    with app.app_context(), \
         patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'k'}), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=paradas), \
         patch('app.services.chatwoot.buscar_historico') as hist, \
         patch('app.services.chatbot._followup_gerar_texto') as gerar, \
         patch('app.services.chatwoot.enviar_mensagem') as env:
        _marcar()
        res = chatbot.followup_conversas_paradas()
    assert res == {'avaliadas': 0, 'enviadas': 0}
    hist.assert_not_called()
    gerar.assert_not_called()
    env.assert_not_called()


def test_vassoura_com_humano_presente_abre_e_nao_responde(app):
    from app.services import chatbot
    paradas = [{'id': 7, 'minutos_paradas': 20, 'nome_contato': 'Ale',
                'telefone': '+5511910935006'}]
    api_hist = [{'role': 'user', 'content': 'alguém?'}]
    with app.app_context(), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=paradas), \
         patch('app.services.chatwoot.buscar_historico', return_value=api_hist), \
         patch('app.services.chatbot.responder') as resp, \
         patch('app.services.chatwoot.enviar_mensagem') as env, \
         patch('app.services.chatwoot.definir_status',
               return_value={'ok': True}) as st:
        _marcar()
        res = chatbot.varrer_pendentes_sem_resposta()
    assert res == {'varridas': 0, 'respondidas': 0}
    resp.assert_not_called()
    env.assert_not_called()
    st.assert_called_once_with(7, 'open')


# ── 4. Contenção da espera humana ──

def test_contencao_nao_sai_com_nota_privada_mas_dono_e_avisado(app):
    """O caso real: dono reabre + nota privada às 19:06; às 19:20 o sistema
    NÃO manda "alta demanda" ao contato. O alerta ao dono segue (cobrança
    até resolver)."""
    from app.services import chatbot_vigia
    base = {'id': 2409, 'nome_contato': 'Ale', 'minutos_paradas': 15}
    hist = [{'role': 'user', 'content': 'Me liga por favor'}]
    with app.app_context(), \
         patch('app.services.chatbot_vigia._numero_destino',
               return_value='5511999990000'), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=[base]), \
         patch('app.services.chatwoot.buscar_historico', return_value=hist), \
         patch('app.services.chatwoot.enviar_mensagem') as contem, \
         patch('app.services.zapi.enviar_texto',
               return_value={'ok': True}) as alerta:
        _marcar('2409', autor='Caio Antinhani')
        chatbot_vigia.alertar_clientes_esperando_humano()
    contem.assert_not_called()
    alerta.assert_called_once()


def test_contencao_le_a_api_quando_o_webhook_da_nota_nao_chegou(app):
    """Rede de segurança: sem marcador no banco, a contenção consulta a
    API do Chatwoot; nota privada humana recente lá = silêncio + marcador
    persistido."""
    from app.models import PresencaHumanaConversa
    from app.services import chatbot_vigia
    base = {'id': 2410, 'nome_contato': 'Ale', 'minutos_paradas': 15}
    hist = [{'role': 'user', 'content': 'Me liga por favor'}]
    na_api = {'quando': agora() - timedelta(minutes=14), 'autor': 'Caio'}
    with app.app_context(), \
         patch('app.services.chatbot_vigia._numero_destino',
               return_value='5511999990000'), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=[base]), \
         patch('app.services.chatwoot.buscar_historico', return_value=hist), \
         patch('app.services.chatwoot.nota_privada_humana_recente',
               return_value=na_api) as leitura, \
         patch('app.services.chatwoot.enviar_mensagem') as contem, \
         patch('app.services.zapi.enviar_texto', return_value={'ok': True}):
        chatbot_vigia.alertar_clientes_esperando_humano()
        row = app.extensions['sqlalchemy'].session.get(PresencaHumanaConversa, '2410')
        assert row is not None and row.autor == 'Caio'
    leitura.assert_called_once()
    contem.assert_not_called()


def test_contencao_sai_normalmente_sem_nota(app):
    """Regressão: sem nota privada a contenção continua saindo (contrato de
    09/08/2026)."""
    from app.services import chatbot_vigia
    base = {'id': 2411, 'nome_contato': 'Bia', 'minutos_paradas': 15}
    hist = [{'role': 'user', 'content': 'Vocês têm cesta de café?'}]
    with app.app_context(), \
         patch('app.services.chatbot_vigia._numero_destino',
               return_value='5511999990000'), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=[base]), \
         patch('app.services.chatwoot.buscar_historico', return_value=hist), \
         patch('app.services.chatwoot.nota_privada_humana_recente',
               return_value=None), \
         patch('app.services.chatwoot.enviar_mensagem',
               return_value={'ok': True}) as contem, \
         patch('app.services.zapi.enviar_texto', return_value={'ok': True}):
        chatbot_vigia.alertar_clientes_esperando_humano()
    contem.assert_called_once()
    assert 'já vai te responder' in contem.call_args[0][1]


# ── 5. Helpers do Chatwoot ──

def test_remetente_humano():
    from app.services import chatwoot
    assert chatwoot.remetente_humano({'sender': {'type': 'user'}})
    assert chatwoot.remetente_humano({'sender_type': 'User'})
    assert not chatwoot.remetente_humano({'sender': {'type': 'agent_bot'}})
    assert not chatwoot.remetente_humano({'sender': {'type': 'contact'}})
    assert not chatwoot.remetente_humano({'sender': {'type': 'user'},
                                          'content_attributes': {'automation_rule_id': 1}})
    assert not chatwoot.remetente_humano({})


def test_nota_privada_humana_recente_le_a_listagem(app):
    """Só nota PRIVADA de HUMANO dentro da janela conta; a mais recente vence."""
    import time

    from app.services import chatwoot
    agora_ts = time.time()
    payload = [
        {'id': 1, 'private': True, 'created_at': agora_ts - 3600 * 20,
         'sender': {'type': 'user', 'name': 'Velha'}},          # fora da janela
        {'id': 2, 'private': True, 'created_at': agora_ts - 600,
         'sender': {'type': 'agent_bot', 'name': 'Bot'}},       # bot
        {'id': 3, 'private': False, 'created_at': agora_ts - 300,
         'sender': {'type': 'user', 'name': 'Publica'}},        # pública
        {'id': 4, 'private': True, 'created_at': agora_ts - 900,
         'sender': {'type': 'user', 'name': 'Caio'}},
        {'id': 5, 'private': True, 'created_at': agora_ts - 120,
         'sender': {'type': 'user', 'name': 'Ana'}},
    ]
    fake = MagicMock(status_code=200, text='x')
    fake.json.return_value = {'payload': payload}
    with app.app_context():
        app.config['CHATWOOT_URL'] = 'https://x.example'
        app.config['CHATWOOT_ACCOUNT_ID'] = '1'
        app.config['CHATWOOT_API_TOKEN'] = 'tok'
        with patch('app.services.chatwoot.requests.get', return_value=fake):
            res = chatwoot.nota_privada_humana_recente(2409, horas=12)
            fake.json.return_value = {'payload': payload[:3]}
            nada = chatwoot.nota_privada_humana_recente(2409, horas=12)
    assert res['autor'] == 'Ana'
    assert (agora() - res['quando']).total_seconds() < 300
    assert nada is None


def test_humano_presente_nao_consulta_api_por_padrao(app):
    from app.services import presenca_humana
    with app.app_context(), \
         patch('app.services.chatwoot.nota_privada_humana_recente') as leitura:
        assert not presenca_humana.humano_presente('55')
    leitura.assert_not_called()


# ── 6. Gate `somente_bot` (follow-up/vassoura) lê a nota da própria listagem ──

def _listagem(nota_humana=True, minutos=10, sender_type='user', **extra):
    import time
    agora_ts = time.time()
    msgs = [
        {'id': 1, 'message_type': 'incoming', 'content': 'oi',
         'created_at': agora_ts - 1200, 'sender': {'type': 'contact'}},
        {'id': 2, 'message_type': 'outgoing', 'content': 'Olá! Como posso ajudar?',
         'created_at': agora_ts - 1100, 'sender': {'type': 'agent_bot'}},
    ]
    if nota_humana:
        msgs.append({'id': 3, 'message_type': 'outgoing', 'private': True,
                     'content': '@Painel', 'created_at': agora_ts - minutos * 60,
                     'sender': {'type': sender_type, 'name': 'Caio'}, **extra})
    return msgs


def _cfg_chatwoot(app):
    app.config['CHATWOOT_URL'] = 'https://x.example'
    app.config['CHATWOOT_ACCOUNT_ID'] = '1'
    app.config['CHATWOOT_API_TOKEN'] = 'tok'


def test_somente_bot_devolve_vazio_com_nota_privada_humana_e_persiste(app):
    """Follow-up e vassoura param ANTES de gastar modelo, mesmo que o
    webhook da nota nunca tenha chegado: a listagem crua tem a nota."""
    from app.models import PresencaHumanaConversa
    from app.services import chatwoot
    fake = MagicMock(status_code=200, text='x')
    fake.json.return_value = {'payload': _listagem()}
    with app.app_context():
        _cfg_chatwoot(app)
        with patch('app.services.chatwoot.requests.get', return_value=fake), \
             patch('app.services.chatwoot.consultar_conversa') as status:
            hist = chatwoot.buscar_historico(77, incluir_autoria=True, somente_bot=True)
        row = app.extensions['sqlalchemy'].session.get(PresencaHumanaConversa, '77')
    assert hist == []
    status.assert_not_called()          # nem precisou reconsultar o status
    assert row is not None and row.autor == 'Caio'


def test_somente_bot_ignora_nota_de_automacao_e_nota_velha(app):
    from app.models import PresencaHumanaConversa
    from app.services import chatwoot, presenca_humana
    fake = MagicMock(status_code=200, text='x')
    with app.app_context():
        _cfg_chatwoot(app)
        for listagem in (_listagem(sender_type='agent_bot'),
                         _listagem(content_attributes={'automation_rule_id': 9}),
                         _listagem(minutos=(presenca_humana.PRESENCA_HUMANA_HORAS + 1) * 60)):
            fake.json.return_value = {'payload': listagem}
            with patch('app.services.chatwoot.requests.get', return_value=fake), \
                 patch('app.services.chatwoot.consultar_conversa',
                       return_value={'status': 'pending'}):
                hist = chatwoot.buscar_historico(78, incluir_autoria=True,
                                                 somente_bot=True)
            assert hist and hist[-1]['role'] == 'assistant'
        assert app.extensions['sqlalchemy'].session.get(
            PresencaHumanaConversa, '78') is None


def test_buscar_historico_normal_segue_descartando_a_nota(app):
    """Sem `somente_bot` o histórico pro Claude continua sem notas (a nota
    nunca vira turno do modelo) e nada é persistido."""
    from app.models import PresencaHumanaConversa
    from app.services import chatwoot
    fake = MagicMock(status_code=200, text='x')
    fake.json.return_value = {'payload': _listagem()}
    with app.app_context():
        _cfg_chatwoot(app)
        with patch('app.services.chatwoot.requests.get', return_value=fake):
            hist = chatwoot.buscar_historico(79)
        assert [m['role'] for m in hist] == ['user', 'assistant']
        assert app.extensions['sqlalchemy'].session.get(
            PresencaHumanaConversa, '79') is None


# ── 7. Episódio humano ENCERRADO: resolvida / devolvida ao bot (revisão 21/09) ──

def test_encerrar_libera_o_bot_e_a_mesma_nota_relida_nao_reabre(app):
    from app.services import presenca_humana as ph
    with app.app_context():
        assert _marcar() is True
        assert ph.humano_presente('7')
        assert ph.encerrar('7', 'teste') is True
        assert not ph.humano_presente('7')
        assert ph.encerrar('7') is False           # idempotente
        # A MESMA nota relida pela API (mesmo instante) não reabre.
        nota_em, aberto = ph.estado('7')
        with patch('app.services.chatwoot.nota_privada_humana_recente',
                   return_value={'quando': nota_em, 'autor': 'Caio'}):
            assert not ph.humano_presente('7', consultar_chatwoot=True)
        # Nota MAIS NOVA reabre.
        with patch('app.services.chatwoot.nota_privada_humana_recente',
                   return_value={'quando': nota_em + timedelta(minutes=1), 'autor': 'Ana'}):
            assert ph.humano_presente('7', consultar_chatwoot=True)
        _, aberto = ph.estado('7')
        assert aberto


def test_evento_de_status_resolved_ou_pending_encerra_o_episodio(app):
    """"Devolvida pro bot" (pending) ou resolver no Chatwoot: o bot volta
    a responder na mensagem seguinte; status 'open' não encerra."""
    from app.services import presenca_humana as ph
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    c = app.test_client()
    with app.app_context():
        _marcar(horas_atras=1)     # nota de 1h atrás (fora da tolerância de 90s)
        r = c.post('/crm/bot?k=seg', json={'event': 'conversation_status_changed',
                                          'conversation': {'id': 7, 'status': 'open'}})
        assert r.get_json().get('episodio') is None
        assert ph.humano_presente('7')
        # payload PLANO (formato real do evento: status/id no topo)
        r = c.post('/crm/bot?k=seg', json={'event': 'conversation_status_changed',
                                          'id': 7, 'status': 'pending'})
        assert r.get_json()['episodio'] == 'encerrado'
        assert not ph.humano_presente('7')
    with patch('threading.Thread', _SyncThread), \
         patch('app.services.chatwoot.buscar_historico', return_value=[]), \
         patch('app.services.chatbot.responder',
               return_value={'acao': 'responder', 'texto': 'Oi!'}) as resp, \
         patch('app.services.chatwoot.enviar_mensagem', return_value={'ok': True}) as env, \
         patch('app.services.chatwoot.definir_status') as st:
        c.post('/crm/bot?k=seg', json=_incoming())
    resp.assert_called_once()
    env.assert_called_once()
    st.assert_not_called()


def test_painel_devolver_ao_bot_ou_resolver_encerra(app, monkeypatch):
    from app.extensions import db
    from app.models import Usuario
    from app.services import chatwoot
    from app.services import presenca_humana as ph
    monkeypatch.setattr(chatwoot, 'definir_status', lambda cid, status, **k: {'ok': True})
    u = Usuario(nome='Dono', login='dono_np', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    with app.app_context():
        _marcar()
        c.post('/entregas/api/atendimento/conversa/7/status', json={'status': 'open'})
        assert ph.humano_presente('7')
        c.post('/entregas/api/atendimento/conversa/7/status', json={'status': 'pending'})
        assert not ph.humano_presente('7')
        _marcar('7', autor='Ana')
        c.post('/entregas/api/atendimento/conversa/7/status', json={'status': 'resolved'})
        assert not ph.humano_presente('7')


def test_candidatos_encerra_quando_ve_resolved(app):
    from app.extensions import db
    from app.models import EsperaAtendimento
    from app.services import atendimento_pendente
    from app.services import presenca_humana as ph
    with app.app_context(), \
         patch('app.services.chatwoot.listar_conversas_paradas', return_value=[]), \
         patch('app.services.chatwoot.consultar_conversa',
               return_value={'status': 'resolved'}):
        _marcar('44')
        db.session.add(EsperaAtendimento(conversa_id='44', inicio_em=agora(),
                                         nome='Ale', mensagem='m', grave=True,
                                         estado='aguardando'))
        db.session.commit()
        atendimento_pendente.candidatos()
        assert not ph.humano_presente('44')


def test_nota_escrita_durante_o_turno_descarta_a_resposta(app):
    """Corrida da revisão 21/09: a checagem antes do modelo passou, o humano
    anotou enquanto o Claude pensava — a resposta não sai."""
    from app.services import chatbot
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    c = app.test_client()

    def _responder_e_anotar(*a, **k):
        _marcar()
        return {'acao': 'responder', 'texto': 'Olá! Posso ajudar?'}

    with patch('threading.Thread', _SyncThread), \
         patch('app.services.chatwoot.buscar_historico', return_value=[]), \
         patch('app.services.chatbot.responder', side_effect=_responder_e_anotar), \
         patch('app.services.chatwoot.enviar_mensagem') as env, \
         patch('app.services.chatwoot.definir_status',
               return_value={'ok': True}) as st, \
         patch('app.services.chatbot_vigia.avaliar') as vigia:
        c.post('/crm/bot?k=seg', json=_incoming())
    env.assert_not_called()
    st.assert_called_once_with(7, 'open')
    vigia.assert_not_called()
    with app.app_context():
        assert chatbot.carregar_historico('7')[-1]['content'] == 'oi'


def test_fallback_de_excecao_nao_fala_com_humano_presente(app):
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    c = app.test_client()
    with app.app_context():
        _marcar()
    # O marcador barra antes do modelo; força a exceção ANTES da checagem
    # (carregar_historico) pra exercitar o fallback do except.
    with patch('threading.Thread', _SyncThread), \
         patch('app.services.chatbot.carregar_historico', side_effect=RuntimeError('db')), \
         patch('app.services.chatwoot.enviar_mensagem') as env, \
         patch('app.services.chatwoot.definir_status',
               return_value={'ok': True}) as st:
        c.post('/crm/bot?k=seg', json=_incoming())
    env.assert_not_called()
    st.assert_called_with(7, 'open')


def test_vassoura_le_a_api_antes_do_gate_e_abre(app):
    """Webhook da nota perdido + bot morto + cliente esperando: a vassoura
    acha a nota pela API, abre a conversa e não responde (revisão 21/09:
    o ramo antigo era inalcançável porque o gate devolvia [] antes)."""
    from app.services import chatbot
    paradas = [{'id': 7, 'minutos_paradas': 20, 'nome_contato': 'Ale',
                'telefone': '+5511910935006'}]
    na_api = {'quando': agora() - timedelta(minutes=5), 'autor': 'Caio'}
    with app.app_context(), \
         patch('app.services.chatwoot.listar_conversas_paradas', return_value=paradas), \
         patch('app.services.chatwoot.nota_privada_humana_recente', return_value=na_api), \
         patch('app.services.chatwoot.buscar_historico') as hist, \
         patch('app.services.chatbot.responder') as resp, \
         patch('app.services.chatwoot.enviar_mensagem') as env, \
         patch('app.services.chatwoot.definir_status',
               return_value={'ok': True}) as st:
        res = chatbot.varrer_pendentes_sem_resposta()
    assert res == {'varridas': 0, 'respondidas': 0}
    hist.assert_not_called()
    resp.assert_not_called()
    env.assert_not_called()
    st.assert_called_once_with(7, 'open')


def test_remetente_humano_tolera_sender_torto():
    from app.services import chatwoot
    assert not chatwoot.remetente_humano({'sender': ['x']})
    assert not chatwoot.remetente_humano({'sender': 'user'})
    assert chatwoot.remetente_humano({'sender': None, 'sender_type': 'user'})


def test_enviar_nota_privada_apaga_se_o_chatwoot_nao_honrou_private(app):
    from unittest.mock import MagicMock

    from app.services import chatwoot
    fake = MagicMock(status_code=200, text='{"id": 55, "private": false}')
    fake.json.return_value = {'id': 55, 'private': False}
    with app.app_context():
        app.config['CHATWOOT_URL'] = 'https://x.example'
        app.config['CHATWOOT_ACCOUNT_ID'] = '1'
        app.config['CHATWOOT_BOT_TOKEN'] = 'bot-tok'
        with patch('app.services.chatwoot.requests.post', return_value=fake), \
             patch('app.services.chatwoot.requests.delete') as delete, \
             patch('app.services.chatwoot.logger') as log:
            res = chatwoot.enviar_nota_privada(2409, 'x')
    assert res == {'ok': False, 'erro': 'nao_privada'}
    delete.assert_called_once()
    assert delete.call_args[0][0].endswith('/messages/55')
    log.error.assert_called_once()


# ── 8. Sonda: o marcador é visível de fora ──

def test_sonda_vigia_vereditos_expoe_presenca_humana(app):
    app.config['CLAUDE_API_TOKEN'] = 'tok-sonda'
    c = app.test_client()
    with app.app_context():
        _marcar('2409', autor='Caio Antinhani')
    r = c.get('/api/claude/vigia-vereditos?conversa=2409',
              headers={'Authorization': 'Bearer tok-sonda'})
    assert r.status_code == 200
    pres = r.get_json()['conversa']['presenca_humana']
    assert pres['autor'] == 'Caio Antinhani' and pres['notas'] == 1
    assert pres['nota_em'] and pres['aberta_em'] is None
    r2 = c.get('/api/claude/vigia-vereditos?conversa=1',
               headers={'Authorization': 'Bearer tok-sonda'})
    assert r2.get_json()['conversa']['presenca_humana'] is None

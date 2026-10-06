"""Mensagem que o WhatsApp não entregou (`type: unsupported`) — caso 2339.

De 15 a 29/09/2026 um mesmo contato mandou sete mensagens que o WhatsApp
(Cloud API) entregou como `unsupported`; o Chatwoot gravou o placeholder
"This message is unavailable." (`content_attributes.is_unsupported`) e o
sistema tratou o texto em inglês como fala do cliente: o bot pedia "me
conte em texto", o atendimento restrito encaminhava com "vou passar sua
mensagem" e a equipe recebia a conversa sem saber o que era. Contrato:
o placeholder vira `chatwoot.MARCADOR_MENSAGEM_INDISPONIVEL` em todo
caminho que lê a mensagem; o cliente recebe o motivo e o gesto que
resolve; a equipe recebe o marcador e o motivo na nota do handoff.
"""
import json
import time
from unittest.mock import patch

import pytest

from app.extensions import db
from app.services import atendimento_restrito as atendimento
from app.services import chatwoot
from app.services.atendimento_humano import POLITICA_ATENDIMENTO

PLACEHOLDER_EN = 'This message is unavailable.'
PLACEHOLDER_PT = 'Esta mensagem não está disponível.'
MARCADOR = chatwoot.MARCADOR_MENSAGEM_INDISPONIVEL


class _SyncThread:
    def __init__(self, target=None, **kwargs):
        self.target = target

    def start(self):
        self.target()


def _m(texto, **extras):
    return {'role': 'user', 'content': texto, **extras}


@pytest.fixture
def sem_motor(monkeypatch):
    def proibido(*args, **kwargs):
        pytest.fail('Atendimento restrito chamou modelo ou ferramenta de negócio')
    monkeypatch.setattr('anthropic.Anthropic', proibido)
    monkeypatch.setattr('app.services.chatbot._executar_tool', proibido)
    monkeypatch.setattr('app.services.chatbot._fora_horario_chat', lambda: False)


@pytest.fixture
def canais(app):
    app.config.update(CHATWOOT_BOT_SECRET='seg', CHATWOOT_URL='https://atendimento.test',
                      CHATWOOT_ACCOUNT_ID='1', CHATWOOT_BOT_TOKEN='bot')
    with patch('threading.Thread', _SyncThread), \
            patch('app.services.chatwoot.buscar_historico', return_value=[]), \
            patch('app.services.chatwoot.consultar_conversa', return_value={'status': 'pending'}), \
            patch('app.services.chatwoot.enviar_mensagem', return_value={'ok': True}) as enviar, \
            patch('app.services.chatwoot.definir_status', return_value={'ok': True}) as status, \
            patch('app.services.entrega_candidata.anotar_handoff', return_value={'ok': True}) as nota, \
            patch('app.services.chatbot_vigia.disponivel', return_value=False), \
            patch('app.services.chatbot._fora_horario_chat', return_value=False):
        yield app.test_client(), enviar, status, nota


def _post(client, content, **extra):
    payload = {'event': 'message_created', 'message_type': 'incoming',
               'conversation': {'id': 2339, 'status': 'pending'},
               'sender': {'name': 'Cliente'}, 'content': content}
    payload.update(extra)
    return client.post('/crm/bot?k=seg', json=payload)


# ── 1. Detector (fonte única, payload do webhook e listagem da API) ──────

@pytest.mark.parametrize('msg, esperado', [
    ({'message_type': 'incoming', 'content': PLACEHOLDER_EN,
      'content_attributes': {'is_unsupported': True}}, True),
    # O atributo é o contrato: vale mesmo que o texto do placeholder mude.
    ({'message_type': 0, 'content': 'qualquer texto',
      'content_attributes': {'is_unsupported': True}}, True),
    # Chatwoot sem o atributo (versão antiga): o texto exato ainda pega.
    ({'message_type': 'incoming', 'content': PLACEHOLDER_EN}, True),
    ({'message_type': 'incoming', 'content': PLACEHOLDER_PT}, True),
    ({'message_type': 'incoming', 'content': '  THIS message is unavailable.  '}, True),
    # Texto real que contém a frase não é placeholder.
    ({'message_type': 'incoming',
      'content': 'This message is unavailable. Pode me ligar?'}, False),
    ({'message_type': 'incoming', 'content': 'oi',
      'content_attributes': {'is_unsupported': False}}, False),
    # Mensagem NOSSA nunca conta, nem com o atributo.
    ({'message_type': 'outgoing', 'content': PLACEHOLDER_EN,
      'content_attributes': {'is_unsupported': True}}, False),
    ({'message_type': 1, 'content': PLACEHOLDER_EN}, False),
    ('texto solto', False),
    (None, False),
    # `content_attributes` torto (string/lista/None): decide pelo texto.
    ({'message_type': 'incoming', 'content': PLACEHOLDER_EN,
      'content_attributes': 'x'}, True),
    ({'message_type': 'incoming', 'content': 'oi', 'content_attributes': ['x']}, False),
    ({'message_type': 'incoming', 'content': PLACEHOLDER_PT,
      'content_attributes': None}, True),
])
def test_detector_de_mensagem_indisponivel(msg, esperado):
    assert chatwoot.mensagem_indisponivel(msg) is esperado


@pytest.mark.parametrize('content, esperado', [
    (PLACEHOLDER_EN, True), (PLACEHOLDER_PT, True), ('  this message is unavailable. ', True),
    (PLACEHOLDER_EN + ' Pode me ligar?', False), ('', False), (None, False), (['x'], False),
])
def test_texto_indisponivel_e_a_fonte_unica_do_texto_cru(content, esperado):
    assert chatwoot.texto_indisponivel(content) is esperado


def test_marcador_cabe_no_aviso_ao_dono_com_a_acao_primeiro():
    """`chatbot_vigia.alertar_clientes_esperando_humano` corta a última
    mensagem em 120 caracteres: o marcador inteiro (fato + gesto) precisa
    caber, senão o dono lê a frase cortada e perde "pedir reenvio/ligar"."""
    assert len(MARCADOR) <= 120
    assert MARCADOR.startswith('[mensagem indisponível')
    assert 'reenvio em texto' in MARCADOR and 'ligar' in MARCADOR
    # A causa não é afirmada: o tipo original não chega no evento.
    for palavra in ('temporár', 'enquete', 'visualização', '131051'):
        assert palavra not in MARCADOR


# ── 2. Listagem da API (vassoura, follow-up, espera humana) ─────────────

def test_buscar_historico_troca_o_placeholder_pelo_marcador(app, monkeypatch):
    class _R:
        status_code = 200
        text = '{}'

        def json(self):
            return {'payload': [
                {'message_type': 'incoming', 'content': PLACEHOLDER_EN,
                 'content_attributes': {'is_unsupported': True},
                 'created_at': 1790428190},
                {'message_type': 'outgoing', 'content': 'Obrigada pelo contato.',
                 'created_at': 1790428195},
                # Placeholder pelo TEXTO (sem atributo) também vira marcador.
                {'message_type': 'incoming', 'content': PLACEHOLDER_PT,
                 'created_at': 1790428300},
                # Mensagem NOSSA com o mesmo texto fica como está.
                {'message_type': 'outgoing', 'content': PLACEHOLDER_EN,
                 'created_at': 1790428400},
            ]}

    monkeypatch.setattr(chatwoot, 'disponivel', lambda: True)
    monkeypatch.setattr(chatwoot, '_headers', lambda: {})
    monkeypatch.setattr(chatwoot.requests, 'get', lambda *a, **k: _R())
    with app.app_context():
        h = chatwoot.buscar_historico(2339)
    assert [m['role'] for m in h] == ['user', 'assistant', 'user', 'assistant']
    assert h[0]['content'] == MARCADOR
    assert h[0]['indisponivel'] is True
    assert 'anexos' not in h[0]
    assert h[2]['content'] == MARCADOR
    assert h[3]['content'] == PLACEHOLDER_EN
    assert PLACEHOLDER_EN not in (h[0]['content'] + h[2]['content'])


def test_listar_conversas_mostra_o_marcador_no_preview(app, monkeypatch):
    """A coluna Atendimento do painel (e a sonda atendimento-painel) leem
    esta lista, não a thread — o preview também troca o placeholder."""
    class _R:
        text = '{}'

        def json(self):
            return {'data': {'payload': [
                {'id': 2339, 'status': 'open', 'last_activity_at': 30,
                 'meta': {'sender': {'name': 'Contato'}, 'channel': 'Channel::Whatsapp'},
                 'last_non_activity_message': {
                     'message_type': 0, 'content': PLACEHOLDER_EN,
                     'content_attributes': {'is_unsupported': True}}},
                # Sem last_non_activity_message: fallback pelo array `messages`.
                {'id': 2, 'status': 'open', 'last_activity_at': 20,
                 'meta': {'sender': {'name': 'Outro'}},
                 'messages': [{'message_type': 0, 'content': PLACEHOLDER_PT}]},
                # Mensagem NOSSA com o texto do placeholder fica como está.
                {'id': 3, 'status': 'open', 'last_activity_at': 10,
                 'meta': {'sender': {'name': 'Nossa'}},
                 'last_non_activity_message': {'message_type': 1, 'content': PLACEHOLDER_EN}},
            ]}}

    monkeypatch.setattr(chatwoot, 'disponivel', lambda: True)
    monkeypatch.setattr(chatwoot, '_headers', lambda: {})
    monkeypatch.setattr(chatwoot, '_consultar_conversas', lambda *a, **k: _R())
    with app.app_context():
        out = chatwoot.listar_conversas('open')
    previews = {c['id']: c['preview'] for c in out}
    assert previews[2339] == MARCADOR[:90]
    assert previews[2] == MARCADOR[:90]
    assert previews[3] == PLACEHOLDER_EN


def _semear_store_legado(conv_id='2339', contato_key='12134098546'):
    from app.models import ChatbotConversa
    from app.utils import agora
    legado = [{'role': 'user', 'content': PLACEHOLDER_EN},
              {'role': 'assistant', 'content': 'Pode me contar em texto o que precisa?'},
              {'role': 'user', 'content': PLACEHOLDER_EN},
              {'role': 'assistant', 'content': 'Obrigada pelo contato. Vou passar sua '
               'mensagem para nossa equipe continuar o atendimento por aqui.',
               'handoff_em': '2026-09-26T10:09:55'}]
    db.session.add(ChatbotConversa(conv_id=conv_id, mensagens_json=json.dumps(legado),
                                   ultima_msg_em=agora(), contato_key=contato_key))
    db.session.commit()


def test_store_legado_com_o_placeholder_cru_e_lido_com_o_marcador(app):
    """Conv 2339 real: o store gravado ANTES do fix carrega sete entradas
    "This message is unavailable." como fala do cliente. A leitura (e a
    herança cross-conversa) normalizam pela mesma fonte única."""
    from app.services.chatbot import carregar_historico, contexto_do_contato
    with app.app_context():
        _semear_store_legado()
        hist = carregar_historico(2339)
        falas = [m for m in hist if m['role'] == 'user']
        assert [m['content'] for m in falas] == [MARCADOR, MARCADOR]
        assert all(m['indisponivel'] is True for m in falas)
        assert hist[3]['handoff_em'] == '2026-09-26T10:09:55'   # o resto fica intacto
        herdado = contexto_do_contato('12134098546', excluir_conv='9999')
        assert [m['content'] for m in herdado if m['role'] == 'user'] == [MARCADOR, MARCADOR]
        assert PLACEHOLDER_EN not in json.dumps(herdado, ensure_ascii=False)


# ── 3. Atendimento restrito: texto próprio, nunca "vou passar sua mensagem"

def test_restrito_responde_com_o_motivo_e_o_gesto(app, sem_motor):
    """Dono 06/10/2026 (conv 2402): desconsiderada — pede reenvio, NUNCA
    passa para a equipe nem promete atendimento."""
    out = atendimento.responder([_m(MARCADOR)])
    assert out['acao'] == 'responder'
    assert out['texto'] == atendimento.TEXTO_MENSAGEM_INDISPONIVEL
    assert 'chegou sem conteúdo' in out['texto']
    assert 'texto comum' in out['texto']
    assert 'Vou passar sua mensagem' not in out['texto']
    assert 'equipe' not in out['texto']
    assert 'http' not in out['texto']
    # O tipo original não chega no evento: o texto ao cliente não diagnostica.
    for palavra in ('temporár', 'enquete', 'visualização única', '131051'):
        assert palavra not in out['texto']
    assert out['motivo'] == atendimento.MOTIVO_MENSAGEM_INDISPONIVEL
    assert 'desconsiderada' in out['motivo']
    assert out['tools_usadas'] == []
    assert out['politica_atendimento'] == atendimento.POLITICA


def test_restrito_pede_reenvio_uma_vez_e_depois_ignora(app, sem_motor):
    hist = [_m(MARCADOR), {'role': 'assistant',
                           'content': atendimento.TEXTO_MENSAGEM_INDISPONIVEL},
            _m(MARCADOR)]
    out = atendimento.responder(hist)
    assert out['acao'] == 'ignorar' and out['texto'] == ''
    assert out['motivo'] == atendimento.MOTIVO_INDISPONIVEL_REPETIDA


def test_restrito_reconhece_o_pedido_de_reenvio_antigo(app, sem_motor):
    """O texto de 30/09 ("Nossa equipe continua...") já está nos stores."""
    antigo = ('Sua mensagem chegou sem conteúdo aqui no nosso atendimento. Pode '
              'reenviar como mensagem de texto comum? Nossa equipe continua o '
              'atendimento por aqui.')
    out = atendimento.responder([_m(MARCADOR), {'role': 'assistant', 'content': antigo},
                                 _m(MARCADOR)])
    assert out['acao'] == 'ignorar'


def test_restrito_fala_real_no_meio_zera_o_pedido(app, sem_motor):
    hist = [_m(MARCADOR), {'role': 'assistant',
                           'content': atendimento.TEXTO_MENSAGEM_INDISPONIVEL},
            _m('oi'), {'role': 'assistant', 'content': 'Olá!'}, _m(MARCADOR)]
    out = atendimento.responder(hist)
    assert out['acao'] == 'responder'
    assert out['texto'] == atendimento.TEXTO_MENSAGEM_INDISPONIVEL


def test_restrito_pedido_herdado_de_outra_conversa_nao_conta(app, sem_motor):
    hist = [_m(MARCADOR, herdada=True),
            {'role': 'assistant', 'content': atendimento.TEXTO_MENSAGEM_INDISPONIVEL,
             'herdada': True},
            _m(MARCADOR)]
    assert atendimento.responder(hist)['acao'] == 'responder'


def test_restrito_encaminhamento_antigo_nao_vira_fila(app, sem_motor):
    """Caso 2339/2402: placeholder em conversa já encaminhada antes não volta
    para a equipe — só o pedido de reenvio."""
    hist = [_m(MARCADOR, handoff_em='2026-09-26T10:09:55'),
            {'role': 'assistant', 'content': 'Obrigada pelo contato. Vou passar '
             'sua mensagem para nossa equipe continuar o atendimento por aqui.',
             'handoff_em': '2026-09-26T10:09:55'},
            _m(MARCADOR)]
    out = atendimento.responder(hist)
    assert out['acao'] == 'responder'
    assert out['texto'] == atendimento.TEXTO_MENSAGEM_INDISPONIVEL


def test_restrito_rajada_so_de_marcadores(app, sem_motor):
    out = atendimento.responder([_m(MARCADOR + '\n' + MARCADOR)])
    assert out['texto'] == atendimento.TEXTO_MENSAGEM_INDISPONIVEL


def test_restrito_flag_da_listagem_da_api_tambem_vale(app, sem_motor):
    """A flag decide sozinha: mesmo conteúdo que não é o marcador."""
    out = atendimento.responder([_m(PLACEHOLDER_EN, indisponivel=True)])
    assert out['texto'] == atendimento.TEXTO_MENSAGEM_INDISPONIVEL
    # Fonte única `chatwoot.fala_indisponivel`: o placeholder cru também vale.
    sem_flag = atendimento.responder([_m(PLACEHOLDER_EN)])
    assert sem_flag['acao'] == 'responder'
    assert sem_flag['texto'] == atendimento.TEXTO_MENSAGEM_INDISPONIVEL


def test_restrito_marcador_com_foto_na_rajada_nao_diz_que_nada_chegou(app, sem_motor):
    """Foto sem legenda + placeholder no mesmo debounce: a foto CHEGOU (está
    no Chatwoot e no store). Segue o encaminhamento de anexo, com o fato
    registrado no motivo para a equipe."""
    out = atendimento.responder([_m(MARCADOR, imagens=['data:image/jpeg;base64,xx'])])
    assert out['acao'] == 'handoff'
    assert out['texto'] != atendimento.TEXTO_MENSAGEM_INDISPONIVEL
    assert 'Vou passar sua mensagem' in out['texto']
    assert out['motivo'] == atendimento.MOTIVO_ANEXO_COM_INDISPONIVEL
    assert 'anexo chegou' in out['motivo']


@pytest.mark.parametrize('texto', [
    MARCADOR + '\nQuero 15 lanches e 15 croissants',
    'Quero 15 lanches\n' + MARCADOR,
])
def test_marcador_misturado_com_texto_real_segue_o_fluxo_normal(app, sem_motor, texto):
    out = atendimento.responder([_m(texto)])
    assert out['acao'] == 'handoff'
    assert 'Vou passar sua mensagem' in out['texto']
    assert out['motivo'] != atendimento.MOTIVO_MENSAGEM_INDISPONIVEL


def test_marcador_antigo_nao_muda_a_resposta_a_mensagem_nova(app, sem_motor):
    hist = [_m(MARCADOR), {'role': 'assistant',
                           'content': atendimento.TEXTO_MENSAGEM_INDISPONIVEL},
            _m('Quero 15 lanches')]
    out = atendimento.responder(hist)
    assert out['acao'] == 'handoff'
    assert out['texto'] != atendimento.TEXTO_MENSAGEM_INDISPONIVEL


def test_placeholder_cru_nunca_chega_ao_cliente_como_fala_normal(app, sem_motor):
    """Defesa em profundidade: se o texto em inglês escapar do webhook (ex.
    seed antigo no store), vale como indisponível — nunca responde FAQ."""
    out = atendimento.responder([_m(PLACEHOLDER_EN)])
    assert out['acao'] == 'responder'
    assert out['texto'] == atendimento.TEXTO_MENSAGEM_INDISPONIVEL


def test_fora_do_horario_avisa_sem_prometer_registrar_mensagem(app, monkeypatch):
    """O prefixo genérico ("Vou registrar sua mensagem") mentiria: não há
    mensagem. Texto próprio com a janela, o fato e o gesto."""
    monkeypatch.setattr('app.services.chatbot._fora_horario_chat', lambda: True)
    out = atendimento.responder([_m(MARCADOR)])
    assert out['acao'] == 'responder'
    assert out['texto'].startswith('Estamos fora do nosso horário')
    assert '07:00 às 20:00' in out['texto']
    assert 'chegou sem conteúdo' in out['texto'] and 'texto comum' in out['texto']
    assert 'a partir das 07:00' in out['texto']
    assert 'Vou registrar' not in out['texto'] and 'Vou passar' not in out['texto']
    assert out['motivo'] == atendimento.MOTIVO_MENSAGEM_INDISPONIVEL


# ── 4. Webhook ponta a ponta ────────────────────────────────────────────

@pytest.mark.parametrize('content, extra', [
    (PLACEHOLDER_EN, {'content_attributes': {'is_unsupported': True}}),
    (PLACEHOLDER_PT, {}),
])
def test_webhook_placeholder_pede_reenvio_sem_equipe(app, canais, content, extra):
    """Dono 06/10/2026 (conv 2402): sem fila, sem status `open`, sem nota."""
    from app.models import EsperaAtendimento, VigiaVeredito
    from app.services.chatbot import carregar_historico
    client, enviar, status, nota = canais
    with patch('anthropic.Anthropic') as modelo:
        assert _post(client, content, **extra).status_code == 200
    modelo.assert_not_called()
    assert VigiaVeredito.query.count() == 0
    enviar.assert_called_once()
    assert enviar.call_args.args[1] == atendimento.TEXTO_MENSAGEM_INDISPONIVEL
    assert enviar.call_args.kwargs['politica_atendimento'] == POLITICA_ATENDIMENTO
    assert enviar.call_args.kwargs['finalidade'] is None
    status.assert_not_called()
    nota.assert_not_called()
    with app.app_context():
        hist = carregar_historico(2339)
        assert EsperaAtendimento.query.filter_by(conversa_id='2339').count() == 0
    falas = [m['content'] for m in hist if m['role'] == 'user']
    assert falas == [MARCADOR]
    assert not any(m.get('handoff_em') for m in hist)


def test_webhook_segundo_placeholder_fica_em_silencio(app, canais):
    from app.models import EsperaAtendimento
    from app.services.chatbot import carregar_historico
    client, enviar, status, _ = canais
    _post(client, PLACEHOLDER_EN, content_attributes={'is_unsupported': True})
    n = enviar.call_count
    _post(client, PLACEHOLDER_EN, content_attributes={'is_unsupported': True})
    assert enviar.call_count == n
    status.assert_not_called()
    with app.app_context():
        hist = carregar_historico(2339)
        assert EsperaAtendimento.query.count() == 0
    assert [m['content'] for m in hist if m['role'] == 'user'] == [MARCADOR, MARCADOR]


def test_webhook_placeholder_com_equipe_na_conversa_nao_fala_nem_mexe(app, canais):
    from app.services import atendimento_humano
    client, enviar, status, nota = canais
    with patch.object(atendimento_humano, 'encaminhamento_pendente', return_value=True):
        _post(client, PLACEHOLDER_EN, content_attributes={'is_unsupported': True})
    enviar.assert_not_called()
    status.assert_not_called()
    nota.assert_not_called()


def test_webhook_content_attributes_torto_nao_derruba_e_decide_pelo_texto(app, canais):
    client, enviar, status, nota = canais
    assert _post(client, PLACEHOLDER_EN, content_attributes='texto-solto').status_code == 200
    assert enviar.call_args.args[1] == atendimento.TEXTO_MENSAGEM_INDISPONIVEL


def test_webhook_foto_e_placeholder_na_mesma_rajada_vao_como_anexo(app, canais):
    from app.blueprints.crm.routes import _depositar_pendente
    from app.services.chatbot import carregar_historico
    client, enviar, status, nota = canais
    _depositar_pendente(2339, '', ['data:image/jpeg;base64,xx'])
    assert _post(client, PLACEHOLDER_EN,
                 content_attributes={'is_unsupported': True}).status_code == 200
    enviar.assert_called_once()
    assert enviar.call_args.args[1] != atendimento.TEXTO_MENSAGEM_INDISPONIVEL
    assert 'Vou passar sua mensagem' in enviar.call_args.args[1]
    assert nota.call_args.args[1]['motivo'] == atendimento.MOTIVO_ANEXO_COM_INDISPONIVEL
    with app.app_context():
        hist = carregar_historico(2339)
    assert hist[0]['content'] == MARCADOR   # o store guarda só o texto; a foto fica no Chatwoot


def test_espera_humana_desconsidera_conversa_so_de_placeholders(app):
    """Caso 2402 (06/10/2026): a equipe respondeu e o cliente mandou outra
    mensagem indisponível; o aviso "esperando ATENDENTE há 26 min" seguia.
    Conversa em que o cliente só mandou conteúdo ilegível não é espera."""
    from app.models import EsperaAtendimento
    from app.services import chatbot_vigia
    from app.utils import agora
    with app.app_context():
        db.session.add(EsperaAtendimento(conversa_id='2402', inicio_em=agora(),
                                         estado='aguardando', mensagem=MARCADOR))
        db.session.commit()
        agora_s = time.time()
        historico = [{'role': 'user', 'content': MARCADOR, 'humano': False,
                      'created_at': agora_s - 1800, 'indisponivel': True},
                     {'role': 'assistant', 'content': 'Boa noite, tudo bem?',
                      'humano': True, 'created_at': agora_s - 600},
                     {'role': 'user', 'content': MARCADOR, 'humano': False,
                      'created_at': agora_s - 500, 'indisponivel': True}]
        conversa = {'id': 2402, 'nome_contato': 'Contato', 'minutos_paradas': 30,
                    'status': 'open', 'telefone': '5511978506319'}
        with patch('app.services.chatbot_vigia._numero_destino', return_value='5511999990000'), \
                patch('app.services.chatwoot.listar_conversas_paradas', return_value=[conversa]), \
                patch('app.services.chatwoot.consultar_conversa', return_value={'status': 'open'}), \
                patch('app.services.chatwoot.buscar_historico', return_value=historico), \
                patch('app.services.zapi.enviar_texto', return_value={'ok': True}) as zapi:
            chatbot_vigia.alertar_clientes_esperando_humano(min_minutos=0)
        zapi.assert_not_called()
        row = EsperaAtendimento.query.filter_by(conversa_id='2402').one()
        assert row.estado == 'sem_cliente'


def test_espera_humana_placeholder_nao_reabre_conversa_ja_respondida(app):
    """Texto real respondido pela equipe + placeholder depois: não há espera."""
    from app.services import atendimento_pendente
    agora_s = time.time()
    historico = [{'role': 'user', 'content': 'Quero 10 croissants', 'humano': False,
                  'created_at': agora_s - 3600},
                 {'role': 'assistant', 'content': 'Anotado!', 'humano': True,
                  'created_at': agora_s - 3000},
                 {'role': 'user', 'content': MARCADOR, 'humano': False,
                  'created_at': agora_s - 1800, 'indisponivel': True}]
    with app.app_context():
        out = atendimento_pendente.preparar(
            {'id': 77, 'nome_contato': 'X', 'minutos_paradas': 30}, historico)
    assert out is None


def test_espera_aberta_pelo_handoff_antigo_de_placeholder_fecha(app):
    """Achado da revisão: espera já aberta (antes do deploy) por um
    placeholder, com texto real anterior já respondido, seguia cobrando."""
    from datetime import timedelta

    from app.models import EsperaAtendimento
    from app.services import atendimento_pendente
    from app.utils import agora
    agora_s = time.time()
    with app.app_context():
        db.session.add(EsperaAtendimento(
            conversa_id='88', inicio_em=agora() - timedelta(minutes=30),
            estado='aguardando', mensagem=MARCADOR, proximo_aviso_em=agora()))
        db.session.commit()
        historico = [{'role': 'user', 'content': 'quero bolo', 'humano': False,
                      'created_at': agora_s - 7200},
                     {'role': 'assistant', 'content': 'Claro!', 'humano': True,
                      'created_at': agora_s - 7000},
                     {'role': 'user', 'content': MARCADOR, 'humano': False,
                      'created_at': agora_s - 1800, 'indisponivel': True}]
        out = atendimento_pendente.preparar(
            {'id': 88, 'nome_contato': 'X', 'minutos_paradas': 30}, historico)
        assert out is None
        row = EsperaAtendimento.query.filter_by(conversa_id='88').one()
        assert row.estado == 'sem_cliente' and row.proximo_aviso_em is None
        # Fala REAL nova reabre a espera normalmente.
        historico.append({'role': 'user', 'content': 'oi, e o bolo?', 'humano': False,
                          'created_at': agora_s - 900})
        out = atendimento_pendente.preparar(
            {'id': 88, 'nome_contato': 'X', 'minutos_paradas': 15}, historico)
        assert out is not None and out.estado == 'aguardando'


def test_espera_com_fala_real_no_episodio_continua(app):
    from datetime import timedelta

    from app.models import EsperaAtendimento
    from app.services import atendimento_pendente
    from app.utils import agora
    agora_s = time.time()
    with app.app_context():
        db.session.add(EsperaAtendimento(
            conversa_id='89', inicio_em=agora() - timedelta(minutes=30),
            estado='aguardando', mensagem='quero bolo'))
        db.session.commit()
        historico = [{'role': 'user', 'content': 'quero bolo', 'humano': False,
                      'created_at': agora_s - 1800},
                     {'role': 'user', 'content': MARCADOR, 'humano': False,
                      'created_at': agora_s - 1700, 'indisponivel': True}]
        out = atendimento_pendente.preparar(
            {'id': 89, 'nome_contato': 'X', 'minutos_paradas': 30}, historico)
        assert out is not None and out.estado == 'aguardando'


def test_paginacao_do_historico_nao_para_no_placeholder(app):
    """Achado da revisão: a página recente só com placeholders não pode
    esconder a fala real anterior (senão a espera vira `sem_cliente`)."""
    app.config.update(CHATWOOT_URL='https://atendimento.test', CHATWOOT_ACCOUNT_ID='1',
                      CHATWOOT_API_TOKEN='u')
    pag1 = [{'id': 20, 'message_type': 'incoming', 'content': PLACEHOLDER_EN,
             'content_attributes': {'is_unsupported': True}, 'created_at': 2000}]
    pag2 = [{'id': 10, 'message_type': 'incoming', 'content': 'quero bolo',
             'created_at': 1000}]

    class _R:
        def __init__(self, data):
            self.status_code, self._d, self.text = 200, data, 'x'

        def json(self):
            return {'payload': self._d}

        def raise_for_status(self):
            pass
    chamadas = []

    def fake_get(url, params=None, **kw):
        chamadas.append(params or {})
        return _R(pag2 if (params or {}).get('before') else pag1)
    with app.app_context(), patch('app.services.chatwoot.requests.get', side_effect=fake_get):
        hist = chatwoot.buscar_historico(2402, incluir_autoria=True)
    assert len(chamadas) >= 2
    assert any(m['content'] == 'quero bolo' for m in hist)


def test_fala_indisponivel_fonte_unica():
    assert chatwoot.fala_indisponivel(_m(MARCADOR))
    assert chatwoot.fala_indisponivel(_m(MARCADOR + '\n' + MARCADOR))
    assert chatwoot.fala_indisponivel(_m('x', indisponivel=True))
    assert chatwoot.fala_indisponivel(_m(PLACEHOLDER_EN))
    assert not chatwoot.fala_indisponivel(_m(MARCADOR + '\nQuero pão'))
    assert not chatwoot.fala_indisponivel(_m(MARCADOR, imagens=['data:x']))
    assert not chatwoot.fala_indisponivel({'role': 'assistant', 'content': MARCADOR})


def test_webhook_texto_normal_nao_e_afetado(app, canais):
    from app.services.chatbot import carregar_historico
    client, enviar, _, _ = canais
    _post(client, 'Quero 15 lanches e 15 croissants')
    assert enviar.call_args.args[1] != atendimento.TEXTO_MENSAGEM_INDISPONIVEL
    with app.app_context():
        hist = carregar_historico(2339)
    assert hist[0]['content'] == 'Quero 15 lanches e 15 croissants'

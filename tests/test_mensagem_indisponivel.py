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
    out = atendimento.responder([_m(MARCADOR)])
    assert out['acao'] == 'handoff'
    assert out['texto'] == atendimento.TEXTO_MENSAGEM_INDISPONIVEL
    assert 'chegou sem conteúdo' in out['texto']
    assert 'texto comum' in out['texto']
    assert 'Vou passar sua mensagem' not in out['texto']
    assert 'http' not in out['texto']
    # O tipo original não chega no evento: o texto ao cliente não diagnostica.
    for palavra in ('temporár', 'enquete', 'visualização única', '131051'):
        assert palavra not in out['texto']
    assert out['motivo'] == atendimento.MOTIVO_MENSAGEM_INDISPONIVEL
    assert 'coexistência' in out['motivo'] and 'ligar' in out['motivo']
    assert 'mensagem indisponível' in out['motivo']
    assert out['tools_usadas'] == []
    assert out['politica_atendimento'] == atendimento.POLITICA


def test_restrito_texto_proprio_mesmo_em_conversa_ja_encaminhada(app, sem_motor):
    """Caso 2339: sete placeholders em 15 dias. A continuidade com a equipe
    não pode esconder do cliente o único gesto que resolve (reenviar)."""
    hist = [_m(MARCADOR, handoff_em='2026-09-26T10:09:55'),
            {'role': 'assistant', 'content': 'Obrigada pelo contato. Vou passar '
             'sua mensagem para nossa equipe continuar o atendimento por aqui.',
             'handoff_em': '2026-09-26T10:09:55'},
            _m(MARCADOR)]
    out = atendimento.responder(hist)
    assert out['acao'] == 'handoff'
    assert out['texto'] == atendimento.TEXTO_MENSAGEM_INDISPONIVEL


def test_restrito_rajada_so_de_marcadores(app, sem_motor):
    out = atendimento.responder([_m(MARCADOR + '\n' + MARCADOR)])
    assert out['texto'] == atendimento.TEXTO_MENSAGEM_INDISPONIVEL


def test_restrito_flag_da_listagem_da_api_tambem_vale(app, sem_motor):
    """A flag decide sozinha: mesmo conteúdo que não é o marcador."""
    out = atendimento.responder([_m(PLACEHOLDER_EN, indisponivel=True)])
    assert out['texto'] == atendimento.TEXTO_MENSAGEM_INDISPONIVEL
    sem_flag = atendimento.responder([_m(PLACEHOLDER_EN)])
    assert sem_flag['acao'] == 'handoff'
    assert sem_flag['texto'] != atendimento.TEXTO_MENSAGEM_INDISPONIVEL


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
    seed antigo no store), o restrito ainda encaminha — nunca responde FAQ."""
    out = atendimento.responder([_m(PLACEHOLDER_EN)])
    assert out['acao'] == 'handoff'


def test_fora_do_horario_avisa_sem_prometer_registrar_mensagem(app, monkeypatch):
    """O prefixo genérico ("Vou registrar sua mensagem") mentiria: não há
    mensagem. Texto próprio com a janela, o fato e o gesto."""
    monkeypatch.setattr('app.services.chatbot._fora_horario_chat', lambda: True)
    out = atendimento.responder([_m(MARCADOR)])
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
def test_webhook_placeholder_vira_marcador_texto_proprio_e_equipe(app, canais, content, extra):
    from app.models import EsperaAtendimento, VigiaVeredito
    from app.services.chatbot import carregar_historico
    client, enviar, status, nota = canais
    with patch('anthropic.Anthropic') as modelo:
        assert _post(client, content, **extra).status_code == 200
    modelo.assert_not_called()
    assert VigiaVeredito.query.count() == 0
    # Cliente: o motivo e o gesto, pela política restrita.
    enviar.assert_called_once()
    assert enviar.call_args.args[1] == atendimento.TEXTO_MENSAGEM_INDISPONIVEL
    assert enviar.call_args.kwargs['politica_atendimento'] == POLITICA_ATENDIMENTO
    assert enviar.call_args.kwargs['finalidade'] == 'encaminhamento_inicial'
    # Equipe: conversa na fila + nota privada com o motivo.
    status.assert_called_once_with(2339, 'open')
    nota.assert_called_once()
    assert nota.call_args.args[1]['motivo'] == atendimento.MOTIVO_MENSAGEM_INDISPONIVEL
    # Store e fila de espera guardam o MARCADOR, nunca o texto em inglês.
    with app.app_context():
        hist = carregar_historico(2339)
        row = EsperaAtendimento.query.filter_by(conversa_id='2339').one()
    falas = [m['content'] for m in hist if m['role'] == 'user']
    assert falas == [MARCADOR]
    assert row.estado == 'aguardando'
    assert 'mensagem indisponível' in row.mensagem
    assert PLACEHOLDER_EN not in row.mensagem and PLACEHOLDER_PT not in row.mensagem


def test_webhook_segundo_placeholder_no_mesmo_episodio_fica_com_a_equipe(app, canais):
    """Já encaminhada: o segundo placeholder não repete o texto (a conversa é
    da equipe), mas entra no store e mantém a fila."""
    from app.services.chatbot import carregar_historico
    client, enviar, status, _ = canais
    _post(client, PLACEHOLDER_EN, content_attributes={'is_unsupported': True})
    n = enviar.call_count
    _post(client, PLACEHOLDER_EN, content_attributes={'is_unsupported': True})
    assert enviar.call_count == n
    with app.app_context():
        hist = carregar_historico(2339)
    assert [m['content'] for m in hist if m['role'] == 'user'] == [MARCADOR, MARCADOR]


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


def test_store_legado_nao_leva_o_texto_em_ingles_a_fila_nem_ao_dono(app, canais):
    """Achado da revisão (30/09/2026): o store da conv 2339 já tinha sete
    placeholders crus; `registrar_encaminhamento` grava as 3 últimas falas
    e a cobrança ao dono (`Assunto:`) repetia o inglês a cada 15 min."""
    from app.models import EsperaAtendimento
    from app.services import chatbot_vigia
    client, enviar, status, nota = canais
    with app.app_context():
        _semear_store_legado()
    _post(client, PLACEHOLDER_EN, content_attributes={'is_unsupported': True})
    with app.app_context():
        row = EsperaAtendimento.query.filter_by(conversa_id='2339').one()
        assert row.estado == 'aguardando'
        assert PLACEHOLDER_EN not in row.mensagem
        assert row.mensagem.count(MARCADOR) == 3
        # Equipe respondeu no Chatwoot antes do próximo ciclo: o aviso ao
        # dono ("ainda aberto") sai com `espera.mensagem` — o marcador
        # inteiro cabe nos 120 caracteres.
        agora_s = time.time()
        historico = [{'role': 'user', 'content': MARCADOR, 'humano': False,
                      'created_at': agora_s - 1800, 'indisponivel': True},
                     {'role': 'assistant', 'content': 'Oi! Aqui é a equipe da O Pão.',
                      'humano': True, 'created_at': agora_s - 60}]
        conversa = {'id': 2339, 'nome_contato': 'Contato', 'minutos_paradas': 30,
                    'status': 'open', 'telefone': '12134098546'}
        with patch('app.services.chatbot_vigia._numero_destino', return_value='5511999990000'), \
                patch('app.services.chatwoot.listar_conversas_paradas', return_value=[conversa]), \
                patch('app.services.chatwoot.buscar_historico', return_value=historico), \
                patch('app.services.zapi.enviar_texto', return_value={'ok': True}) as zapi:
            chatbot_vigia.alertar_clientes_esperando_humano(min_minutos=0)
        textos = [c.args[1] for c in zapi.call_args_list]
        assert textos, 'a cobrança ao dono continua até resolver'
        for texto in textos:
            assert PLACEHOLDER_EN not in texto
            assert MARCADOR in texto


def test_webhook_texto_normal_nao_e_afetado(app, canais):
    from app.services.chatbot import carregar_historico
    client, enviar, _, _ = canais
    _post(client, 'Quero 15 lanches e 15 croissants')
    assert enviar.call_args.args[1] != atendimento.TEXTO_MENSAGEM_INDISPONIVEL
    with app.app_context():
        hist = carregar_historico(2339)
    assert hist[0]['content'] == 'Quero 15 lanches e 15 croissants'

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
from unittest.mock import patch

import pytest

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
])
def test_detector_de_mensagem_indisponivel(msg, esperado):
    assert chatwoot.mensagem_indisponivel(msg) is esperado


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


# ── 3. Atendimento restrito: texto próprio, nunca "vou passar sua mensagem"

def test_restrito_responde_com_o_motivo_e_o_gesto(app, sem_motor):
    out = atendimento.responder([_m(MARCADOR)])
    assert out['acao'] == 'handoff'
    assert out['texto'] == atendimento.TEXTO_MENSAGEM_INDISPONIVEL
    assert 'chegou sem conteúdo' in out['texto']
    assert 'texto comum' in out['texto']
    assert 'Vou passar sua mensagem' not in out['texto']
    assert 'http' not in out['texto']
    assert out['motivo'] == atendimento.MOTIVO_MENSAGEM_INDISPONIVEL
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
    out = atendimento.responder([_m(MARCADOR, indisponivel=True)])
    assert out['texto'] == atendimento.TEXTO_MENSAGEM_INDISPONIVEL


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


def test_fora_do_horario_prefixa_o_aviso(app, monkeypatch):
    monkeypatch.setattr('app.services.chatbot._fora_horario_chat', lambda: True)
    out = atendimento.responder([_m(MARCADOR)])
    assert out['texto'].startswith('Estamos fora do nosso horário')
    assert atendimento.TEXTO_MENSAGEM_INDISPONIVEL in out['texto']


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


def test_webhook_texto_normal_nao_e_afetado(app, canais):
    from app.services.chatbot import carregar_historico
    client, enviar, _, _ = canais
    _post(client, 'Quero 15 lanches e 15 croissants')
    assert enviar.call_args.args[1] != atendimento.TEXTO_MENSAGEM_INDISPONIVEL
    with app.app_context():
        hist = carregar_historico(2339)
    assert hist[0]['content'] == 'Quero 15 lanches e 15 croissants'

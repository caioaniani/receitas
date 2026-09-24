"""A restrição acompanha o envio real, a fila humana e a recuperação."""
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest

from app.services.atendimento_humano import POLITICA_ATENDIMENTO


class _SyncThread:
    def __init__(self, target=None, **kwargs):
        self.target = target

    def start(self):
        self.target()


@pytest.fixture
def canais(app):
    app.config.update(CHATWOOT_BOT_SECRET='seg', CHATWOOT_URL='https://atendimento.test',
                      CHATWOOT_ACCOUNT_ID='1', CHATWOOT_BOT_TOKEN='bot')
    with patch('threading.Thread', _SyncThread), \
            patch('app.services.chatwoot.buscar_historico', return_value=[]), \
            patch('app.services.chatwoot.consultar_conversa', return_value={'status': 'pending'}) as atual, \
            patch('app.services.chatwoot.enviar_mensagem', return_value={'ok': True}) as enviar, \
            patch('app.services.chatwoot.definir_status', return_value={'ok': True}) as status, \
            patch('app.services.entrega_candidata.anotar_handoff', return_value={'ok': True}) as nota, \
            patch('app.services.chatbot_vigia.disponivel', return_value=False):
        yield app.test_client(), atual, enviar, status, nota


def _post(client, content, **extra):
    payload = {'event': 'message_created', 'message_type': 'incoming',
               'conversation': {'id': 919, 'status': 'pending'},
               'sender': {'name': 'Cliente'}, 'content': content}
    payload.update(extra)
    return client.post('/crm/bot?k=seg', json=payload)


def test_compra_tem_fila_duravel_antes_de_qualquer_http(app, canais):
    from app.models import EsperaAtendimento, VigiaVeredito
    from app.services import chatbot
    client, _, enviar, status, _ = canais

    def conferir(*args, **kwargs):
        row = EsperaAtendimento.query.filter_by(conversa_id='919').one()
        assert row.estado == 'aguardando'
        assert not row.grave
        assert chatbot.handoff_recente(919)
        return {'ok': True}

    enviar.side_effect = conferir
    status.side_effect = conferir
    with patch('anthropic.Anthropic') as modelo:
        assert _post(client, 'Quero 15 lanches e 15 croissants').status_code == 200
    modelo.assert_not_called()
    assert VigiaVeredito.query.count() == 0
    status.assert_called_once_with(919, 'open')
    if enviar.called:
        assert enviar.call_args.kwargs['politica_atendimento'] == POLITICA_ATENDIMENTO
        assert enviar.call_args.kwargs['finalidade'] == 'encaminhamento_inicial'


def test_audio_vai_para_equipe_sem_pedir_redigitar(app, canais):
    from app.models import EsperaAtendimento
    client, _, enviar, status, nota = canais
    _post(client, '', attachments=[{'file_type': 'audio', 'data_url': 'https://example.test/audio'}])
    enviar.assert_not_called()
    status.assert_called_once_with(919, 'open')
    nota.assert_called_once()
    assert EsperaAtendimento.query.filter_by(conversa_id='919').one().estado == 'aguardando'


@pytest.mark.parametrize('tipo,legenda', [
    ('audio', 'Bom dia'), ('file', 'Qual o link do site?'),
    ('video', 'Obrigada'),
])
def test_anexo_com_legenda_tambem_vai_para_equipe(app, canais, tipo, legenda):
    from app.models import EsperaAtendimento
    from app.services import chatbot, chatwoot
    client, _, enviar, status, nota = canais
    with patch('app.services.chatbot.responder') as responder:
        _post(client, legenda, attachments=[{'file_type': tipo,
                                           'data_url': 'https://example.test/anexo'}])
    responder.assert_not_called()
    enviar.assert_not_called()
    status.assert_called_once_with(919, 'open')
    hist = chatbot.carregar_historico(919)
    assert legenda in hist[-1]['content']
    assert chatwoot.MARCADOR_ANEXO_CLIENTE in hist[-1]['content']
    assert legenda in EsperaAtendimento.query.filter_by(conversa_id='919').one().mensagem
    assert legenda in nota.call_args.args[2][-1]['content']


@pytest.mark.parametrize('tipo,legenda', [
    ('audio', 'Bom dia'), ('file', 'Qual o link do site?'),
    ('video', 'Obrigada'),
])
def test_recuperacao_preserva_anexo_legendado_com_caption_igual_ao_store(
        app, monkeypatch, tipo, legenda):
    from app.models import EsperaAtendimento
    from app.services import atendimento_humano, chatbot, chatwoot
    app.config.update(CHATWOOT_URL='https://atendimento.test',
                      CHATWOOT_API_TOKEN='teste', CHATWOOT_ACCOUNT_ID='1',
                      LOJA_BASE_URL='https://loja.example.test')
    monkeypatch.setenv('CHATBOT_VASSOURA', '1')
    chatbot.salvar_historico(919, [{'role': 'user', 'content': legenda}], '')
    raw = {'id': 123, 'content': legenda, 'message_type': 'incoming',
           'created_at': 100, 'sender': {'type': 'contact'},
           'attachments': [{'file_type': tipo, 'data_url': 'https://example.test/anexo'}]}
    resposta = Mock(status_code=200, text='json')
    resposta.json.return_value = {'payload': [raw]}
    with patch.object(chatwoot.requests, 'get', return_value=resposta), \
            patch.object(chatwoot, 'consultar_conversa', return_value={'status': 'pending'}), \
            patch.object(chatwoot, 'listar_conversas_paradas', return_value=[{'id': 919, 'minutos_paradas': 20}]), \
            patch.object(chatwoot, 'enviar_mensagem', return_value={'ok': True}) as enviar, \
            patch.object(chatwoot, 'definir_status', return_value={'ok': True}) as status, \
            patch.object(atendimento_humano, 'recuperar_encaminhamentos_pendentes', return_value={}), \
            patch('app.services.presenca_humana.humano_presente', return_value=False), \
            patch('app.services.entrega_candidata.anotar_handoff'), \
            patch('anthropic.Anthropic') as modelo:
        api_hist = chatwoot.buscar_historico(919)
        assert api_hist[-1]['anexos'] is True
        assert legenda in api_hist[-1]['content']
        assert chatwoot.MARCADOR_ANEXO_CLIENTE in api_hist[-1]['content']
        chatbot.varrer_pendentes_sem_resposta()
    modelo.assert_not_called()
    status.assert_called_once_with(919, 'open')
    assert enviar.call_args.kwargs['finalidade'] == 'encaminhamento_inicial'
    assert 'https://' not in enviar.call_args.args[1]
    row = EsperaAtendimento.query.filter_by(conversa_id='919').one()
    assert legenda in row.mensagem
    assert chatwoot.MARCADOR_ANEXO_CLIENTE in row.mensagem
    hist = chatbot.carregar_historico(919)
    assert any(chatwoot.MARCADOR_ANEXO_CLIENTE in m['content'] for m in hist)


def test_audio_atrasado_nao_recria_fila_resolvida(app, canais):
    from app.models import EsperaAtendimento
    client, atual, enviar, status, nota = canais
    atual.return_value = {'status': 'resolved'}
    _post(client, '', attachments=[{'file_type': 'audio'}])
    assert EsperaAtendimento.query.count() == 0
    enviar.assert_not_called()
    status.assert_not_called()
    nota.assert_not_called()


def test_segundo_balao_nao_repete_confirmacao_inicial(app, canais):
    client, _, enviar, _, _ = canais
    _post(client, 'Quero 15 lanches e 15 croissants')
    n = enviar.call_count
    _post(client, 'Pode acrescentar mais um?')
    assert enviar.call_count == n


def test_resposta_nao_enviada_nao_e_avaliada_como_fala_real(app, canais):
    client, _, enviar, _, _ = canais
    enviar.return_value = {'ok': False, 'erro': 'HTTP 503'}
    with patch('app.services.chatbot_vigia.disponivel', return_value=True), \
            patch('app.services.chatbot_vigia.avaliar') as avaliar:
        _post(client, 'Quero 15 lanches e 15 croissants')
    avaliar.assert_called_once()
    hist = avaliar.call_args.args[0]
    assert all(m['role'] == 'user' for m in hist)
    assert avaliar.call_args.kwargs['resultado_bot']['texto'] == ''


@pytest.mark.parametrize('estado', ['open', 'resolved'])
def test_webhook_atrasado_nao_fala_nem_reabre_conversa_humana(app, canais, estado):
    client, atual, enviar, status, _ = canais
    atual.return_value = {'status': estado}
    _post(client, 'Quero complementar meu pedido')
    enviar.assert_not_called()
    status.assert_not_called()


def test_handoff_antigo_persiste_em_silencio_e_nao_resolve_obrigada(app, canais):
    from app.extensions import db
    from app.models import EsperaAtendimento
    from app.utils import agora
    db.session.add(EsperaAtendimento(conversa_id='919', inicio_em=agora() - timedelta(days=3),
                                     estado='aguardando'))
    db.session.commit()
    client, _, enviar, status, _ = canais
    with patch('app.services.chatbot.responder') as responder:
        _post(client, 'Obrigada')
    responder.assert_not_called()
    enviar.assert_not_called()
    status.assert_called_once_with(919, 'open')


def test_falha_status_mantem_fila_visivel_imediatamente(app, canais):
    from app.services import atendimento_pendente
    client, _, _, status, _ = canais
    status.return_value = {'ok': False, 'erro': 'indisponível'}
    _post(client, 'Quero acrescentar mais um croissant')
    alertas = atendimento_pendente.alertas_painel()
    assert len(alertas) == 1
    assert alertas[0]['conv_id'] == '919'
    assert not alertas[0]['grave']


@pytest.mark.parametrize('estado', ['em_atendimento', 'respondido'])
def test_registro_nao_regride_estado_da_equipe(app, estado):
    from app.extensions import db
    from app.models import EsperaAtendimento
    from app.services import atendimento_humano
    from app.utils import agora
    inicio = agora() - timedelta(hours=2)
    db.session.add(EsperaAtendimento(conversa_id='8', inicio_em=inicio, estado=estado))
    db.session.commit()
    assert not atendimento_humano.registrar_encaminhamento(8, [{'role': 'user', 'content': 'Obrigada'}])
    row = db.session.get(EsperaAtendimento, '8')
    assert row.estado == estado
    assert row.inicio_em == inicio


def test_recuperacao_faz_rodizio_e_nunca_reabre_resolvida(app):
    from app.extensions import db
    from app.models import EsperaAtendimento
    from app.services import atendimento_humano
    from app.utils import agora
    for cid in range(1, 7):
        db.session.add(EsperaAtendimento(conversa_id=str(cid), inicio_em=agora(), estado='aguardando'))
    db.session.commit()
    estados = {'1': 'open', '2': 'open', '3': 'resolved', '4': 'pending', '5': 'pending', '6': 'open'}
    with patch('app.services.chatwoot.consultar_conversa', side_effect=lambda cid: {'status': estados[cid]}) as consultar, \
            patch('app.services.chatwoot.definir_status', return_value={'ok': True}) as status, \
            patch('app.services.chatwoot.enviar_mensagem') as enviar:
        for _ in range(3):
            atendimento_humano.recuperar_encaminhamentos_pendentes(limite=2)
    assert {c.args[0] for c in consultar.call_args_list} == set(estados)
    assert {c.args[0] for c in status.call_args_list} == {'4', '5'}
    enviar.assert_not_called()
    assert db.session.get(EsperaAtendimento, '3').estado == 'resolvido'


def test_recuperacao_reconfere_estado_depois_do_lock(app):
    from app.extensions import db
    from app.models import EsperaAtendimento
    from app.services import atendimento_humano
    from app.utils import agora
    db.session.add(EsperaAtendimento(conversa_id='10', inicio_em=agora(), estado='aguardando'))
    db.session.commit()

    @contextmanager
    def equipe_resolveu(cid):
        row = db.session.get(EsperaAtendimento, str(cid))
        row.estado = 'resolvido'
        db.session.commit()
        yield

    with patch('app.blueprints.crm.routes._lock_conv_cross_worker', equipe_resolveu), \
            patch('app.services.chatwoot.consultar_conversa') as consultar, \
            patch('app.services.chatwoot.definir_status') as status:
        atendimento_humano.recuperar_encaminhamentos_pendentes()
    consultar.assert_not_called()
    status.assert_not_called()


@pytest.fixture
def gateway(app):
    app.config.update(CHATWOOT_URL='https://atendimento.test',
                      CHATWOOT_ACCOUNT_ID='1', CHATWOOT_BOT_TOKEN='bot',
                      CHATWOOT_PAINEL_TOKEN='humano')
    with patch('app.services.instancia.pode_falar_com_o_mundo', return_value=True), \
            patch('app.services.presenca_humana.humano_presente', return_value=False) as humano, \
            patch('app.services.chatwoot.consultar_conversa', return_value={'status': 'pending'}) as status, \
            patch('app.services.chatwoot.requests.post', return_value=Mock(status_code=200)) as post:
        yield humano, status, post


def test_gateway_rejeita_followup_e_macro_sem_politica(app, gateway):
    from app.services import chatwoot
    _, _, post = gateway
    assert chatwoot.enviar_mensagem(3, 'Ainda vai querer?')['pulou'] == 'atendimento_restrito'
    assert chatwoot.enviar_mensagem(3, 'Equipe em alta demanda', status_esperado='open')['pulou'] == 'atendimento_restrito'
    post.assert_not_called()


def test_gateway_reconfere_status_e_presenca_na_faq(app, gateway):
    from app.services import chatwoot
    humano, status, post = gateway
    status.return_value = {'status': 'open'}
    assert not chatwoot.enviar_mensagem(3, 'Horários', politica_atendimento=POLITICA_ATENDIMENTO)['ok']
    status.return_value = {'status': 'pending'}
    humano.return_value = True
    assert not chatwoot.enviar_mensagem(3, 'Horários', politica_atendimento=POLITICA_ATENDIMENTO)['ok']
    post.assert_not_called()


def test_gateway_bloqueia_faq_apos_handoff_mas_preserva_wifi_e_painel(app, gateway):
    from app.services import atendimento_humano, chatwoot
    _, _, post = gateway
    atendimento_humano.registrar_encaminhamento(3, [{'role': 'user', 'content': 'Pedido'}])
    assert not chatwoot.enviar_mensagem(3, 'Horários', politica_atendimento=POLITICA_ATENDIMENTO)['ok']
    assert chatwoot.enviar_mensagem(3, 'Código validado', finalidade='wifi_portal')['ok']
    assert chatwoot.enviar_mensagem_painel(3, 'Sou da equipe, vou cuidar do pedido')['ok']
    assert post.call_count == 2


def test_gateway_permite_faq_restrita_em_pending(app, gateway):
    from app.services import chatwoot
    assert chatwoot.enviar_mensagem(3, 'Horários', politica_atendimento=POLITICA_ATENDIMENTO)['ok']


@pytest.mark.parametrize('estado,fila', [
    ('pending', True), ('pending', False), ('open', False), ('resolved', False),
])
def test_wifi_valida_mas_nao_resolve_atendimento_humano(app, canais, estado, fila):
    from app.models import EsperaAtendimento
    from app.services import atendimento_humano
    client, atual, enviar, status, _ = canais
    atual.return_value = {'status': estado}
    if fila:
        atendimento_humano.registrar_encaminhamento(
            919, [{'role': 'user', 'content': 'Preciso complementar o pedido'}])
    with patch('app.services.wifi_portal.processar_codigo_whatsapp',
               return_value={'texto': 'Acesso validado.'}):
        out = _post(client, 'WIFI-AB2345')
    assert out.json['wifi_portal'] is True
    enviar.assert_called_once_with(919, 'Acesso validado.', finalidade='wifi_portal')
    status.assert_not_called()
    if fila:
        assert EsperaAtendimento.query.filter_by(conversa_id='919').one().estado == 'aguardando'


def test_wifi_durante_debounce_nao_descarta_pedido_aguardando(app, canais):
    from app.models import EsperaAtendimento
    client, atual, _, status, _ = canais
    tarefas = []

    class AdiarThread:
        def __init__(self, target=None, **kwargs):
            self.target = target

        def start(self):
            tarefas.append(self.target)

    estado = {'status': 'pending'}
    atual.side_effect = lambda cid: dict(estado)

    def alterar(cid, novo_status):
        estado['status'] = novo_status
        return {'ok': True}

    status.side_effect = alterar
    with patch('threading.Thread', AdiarThread), \
            patch('app.services.wifi_portal.processar_codigo_whatsapp',
                  return_value={'texto': 'Acesso validado.'}):
        _post(client, 'Quero 15 lanches e 15 croissants')
        assert EsperaAtendimento.query.count() == 0
        assert len(tarefas) == 1
        assert _post(client, 'WIFI-AB2345').json['wifi_portal'] is True
        status.assert_not_called()
        tarefas[0]()
    assert estado['status'] == 'open'
    assert EsperaAtendimento.query.filter_by(conversa_id='919').one().estado == 'aguardando'
    assert all(c.args[1] != 'resolved' for c in status.call_args_list)

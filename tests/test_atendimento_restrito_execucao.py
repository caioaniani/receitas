"""Entrada pública, recuperação de pendências e separação do motor offline."""
from unittest.mock import Mock

import pytest

from app.services import atendimento_restrito, chatbot


def _usuario(texto, **extra):
    return {'role': 'user', 'content': texto, **extra}


@pytest.fixture
def sem_modelo(monkeypatch):
    def proibido(*args, **kwargs):
        pytest.fail('Uma entrada pública chamou o motor autônomo')
    monkeypatch.setattr('anthropic.Anthropic', proibido)
    monkeypatch.setattr(chatbot, '_executar_tool', proibido)


def test_wrapper_publico_preserva_politica_sem_modelo(app, sem_modelo, monkeypatch):
    monkeypatch.setitem(app.config, 'TESTING', False)
    monkeypatch.setattr(chatbot, '_responder_modelo_offline',
                        Mock(side_effect=AssertionError('motor antigo')))
    out = chatbot.responder([_usuario('15 lanches e 15 croissants')],
                            telefone_contato='11900000000', conversa_id=901)
    assert out['acao'] == 'handoff'
    assert out['politica_atendimento'] == atendimento_restrito.POLITICA
    assert out['tools_usadas'] == []
    assert 'http' not in out['texto']
    chatbot._responder_modelo_offline.assert_not_called()


@pytest.mark.parametrize('funcao,args', [
    ('_responder_modelo_offline', ([_usuario('oi')],)),
    ('_followup_modelo_offline', ()),
])
def test_motor_privado_recusado_em_producao(app, sem_modelo, monkeypatch, funcao, args):
    monkeypatch.setitem(app.config, 'TESTING', False)
    with pytest.raises(RuntimeError, match='offline'):
        getattr(chatbot, funcao)(*args)


def test_followup_publico_nunca_fala_mesmo_com_config_legada_ativa(app, monkeypatch):
    app.config['CHATBOT_FOLLOWUP'] = '1'
    monkeypatch.setenv('CHATBOT_FOLLOWUP', '1')
    gerar = Mock(side_effect=AssertionError('gerou mensagem'))
    enviar = Mock(side_effect=AssertionError('falou com cliente'))
    listar = Mock(side_effect=AssertionError('procurou cliente para retomar'))
    monkeypatch.setattr(chatbot, '_followup_gerar_texto', gerar)
    monkeypatch.setattr('app.services.chatwoot.enviar_mensagem', enviar)
    monkeypatch.setattr('app.services.chatwoot.listar_conversas_paradas', listar)
    assert 'pulou' in chatbot.followup_conversas_paradas()
    gerar.assert_not_called()
    enviar.assert_not_called()
    listar.assert_not_called()


@pytest.fixture
def vassoura(app, monkeypatch):
    from app.services import atendimento_humano, chatwoot
    hist = [_usuario('Preciso complementar meu pedido com 15 croissants')]
    estado = {'status': 'pending'}
    monkeypatch.setenv('CHATBOT_VASSOURA', '1')
    monkeypatch.setattr(atendimento_humano, 'recuperar_encaminhamentos_pendentes',
                        lambda: {})
    monkeypatch.setattr('app.services.presenca_humana.humano_presente',
                        lambda *args, **kwargs: False)
    monkeypatch.setattr(chatwoot, 'listar_conversas_paradas',
                        lambda **kw: [{'id': 901, 'minutos_paradas': 20,
                                       'nome_contato': 'Cliente',
                                       'telefone': '11900000000'}])
    monkeypatch.setattr(chatwoot, 'buscar_historico', lambda *a, **kw: hist)
    monkeypatch.setattr(chatwoot, 'consultar_conversa', lambda *a, **kw: dict(estado))
    enviar = Mock(return_value={'ok': True})
    status = Mock(return_value={'ok': True})
    monkeypatch.setattr(chatwoot, 'enviar_mensagem', enviar)
    monkeypatch.setattr(chatwoot, 'definir_status', status)
    monkeypatch.setattr('app.services.entrega_candidata.anotar_handoff',
                        Mock(return_value=None))
    return hist, estado, enviar, status


def test_vassoura_handoff_registra_fila_e_marcador_antes_de_falar(app, vassoura, sem_modelo):
    from app.models import EsperaAtendimento
    hist, _, enviar, status = vassoura

    def conferir_antes_do_envio(*args, **kwargs):
        row = EsperaAtendimento.query.filter_by(conversa_id='901').one()
        assert row.estado == 'aguardando'
        assert '15 croissants' in row.mensagem
        assert any(m.get('handoff_em') for m in chatbot.carregar_historico(901))
        return {'ok': True}

    enviar.side_effect = conferir_antes_do_envio
    out = chatbot.varrer_pendentes_sem_resposta()
    assert out['respondidas'] == 1
    assert enviar.call_args.kwargs['politica_atendimento'] == atendimento_restrito.POLITICA
    assert enviar.call_args.kwargs['finalidade'] == 'encaminhamento_inicial'
    status.assert_called_once_with(901, 'open')
    store = chatbot.carregar_historico(901)
    assert store[0]['content'] == hist[0]['content']
    assert any(m.get('handoff_em') for m in store)


def test_vassoura_falha_de_envio_preserva_fila_e_nao_repete_fala(app, vassoura, sem_modelo):
    from app.models import EsperaAtendimento
    _, _, enviar, status = vassoura
    enviar.return_value = {'ok': False, 'erro': 'indisponível'}
    status.return_value = {'ok': False, 'erro': 'indisponível'}
    assert chatbot.varrer_pendentes_sem_resposta()['respondidas'] == 0
    row = EsperaAtendimento.query.filter_by(conversa_id='901').one()
    inicio = row.inicio_em
    assert row.estado == 'aguardando'
    assert any(m.get('handoff_em') for m in chatbot.carregar_historico(901))
    chatbot.varrer_pendentes_sem_resposta()
    enviar.assert_called_once()
    assert row.inicio_em == inicio


@pytest.mark.parametrize('hist', [[], [_usuario('Quero complementar o pedido')]])
def test_marcador_silencioso_sobrevive_a_nova_persistencia(app, hist):
    chatbot.salvar_historico(901, hist, '', handoff=True)
    store = chatbot.carregar_historico(901)
    assert any(m.get('handoff_em') for m in store)
    chatbot.salvar_historico(901, store, '')
    assert any(m.get('handoff_em') for m in chatbot.carregar_historico(901))


@pytest.mark.parametrize('novo_status', ['resolved', 'snoozed'])
def test_vassoura_nao_reabre_se_equipe_muda_status_durante_envio(
        app, vassoura, sem_modelo, novo_status):
    _, estado, enviar, status = vassoura

    def equipe_assumiu(*args, **kwargs):
        estado['status'] = novo_status
        return {'ok': False, 'pulou': 'status_nao_confirmado'}

    enviar.side_effect = equipe_assumiu
    chatbot.varrer_pendentes_sem_resposta()
    status.assert_not_called()


def test_vassoura_caption_igual_ao_store_nao_descarta_imagem(app, vassoura, sem_modelo):
    from app.models import EsperaAtendimento
    hist, _, enviar, _ = vassoura
    chatbot.salvar_historico(901, [_usuario('oi')], '')
    hist[:] = [_usuario('oi', imagens=[{'mimetype': 'image/jpeg', 'base64': 'img'}])]
    chatbot.varrer_pendentes_sem_resposta()
    row = EsperaAtendimento.query.filter_by(conversa_id='901').first()
    assert row is not None, 'Imagem com legenda repetida precisa de atendimento humano'
    assert enviar.call_args.kwargs['finalidade'] == 'encaminhamento_inicial'


def test_vassoura_falha_em_fila_humana_nao_aborta_outra_conversa(
        app, vassoura, sem_modelo, monkeypatch):
    from app.services import atendimento_humano
    _, _, enviar, _ = vassoura
    monkeypatch.setattr('app.services.chatwoot.listar_conversas_paradas', lambda **kw: [
        {'id': 900, 'minutos_paradas': 20}, {'id': 901, 'minutos_paradas': 20}])
    monkeypatch.setattr('app.services.presenca_humana.humano_presente',
                        lambda conv_id, **kw: conv_id == 900)
    registrar = atendimento_humano.registrar_encaminhamento

    def falha_na_primeira(conv_id, *args, **kwargs):
        if conv_id == 900:
            raise RuntimeError('Falha localizada ao registrar espera')
        return registrar(conv_id, *args, **kwargs)

    monkeypatch.setattr(atendimento_humano, 'registrar_encaminhamento', falha_na_primeira)
    assert chatbot.varrer_pendentes_sem_resposta()['respondidas'] == 1
    assert enviar.call_args.args[0] == 901


def test_job_periodico_so_recupera_pendencias(app, monkeypatch):
    from app.services import seru_cron
    varrer = Mock(return_value={})
    followup = Mock(side_effect=AssertionError('retomada automática'))
    monkeypatch.setattr(chatbot, 'varrer_pendentes_sem_resposta', varrer)
    monkeypatch.setattr(chatbot, 'followup_conversas_paradas', followup)
    monkeypatch.setattr(chatbot, '_followup_modelo_offline', followup)
    monkeypatch.setattr('app.services.chatwoot.bot_disponivel', lambda: True)
    monkeypatch.setattr(seru_cron, '_com_lock', lambda chave, funcao, descricao: funcao())
    seru_cron._run_followup_bot(app)
    varrer.assert_called_once()
    followup.assert_not_called()

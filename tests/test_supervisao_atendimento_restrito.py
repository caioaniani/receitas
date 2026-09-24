"""A supervisão acompanha a equipe sem pressionar a autonomia do bot."""
import json
from datetime import timedelta
from unittest.mock import patch

import pytest

from app.services import chatbot_auditor, chatbot_vigia


def _resultado(acao='handoff', **extra):
    return {'acao': acao, 'tools_usadas': [],
            'politica_atendimento': chatbot_vigia.POLITICA_ATENDIMENTO_RESTRITA,
            **extra}


@pytest.mark.parametrize('mensagem', [
    'Quero 15 lanches e 15 croissants para amanhã',
    'Preciso acrescentar dois croissants no pedido que já fiz',
    'Qual o tamanho dos croissants?',
    'E se meu pedido não chegar?',
    'Não veio errado, estava tudo ótimo',
])
def test_encaminhamento_restrito_nao_e_preguica_nem_consulta_modelo(app, mensagem):
    hist = [{'role': 'user', 'content': mensagem}]
    with app.app_context(), \
         patch.object(chatbot_vigia, '_chamar_modelo') as modelo, \
         patch('app.services.zapi.enviar_texto') as alerta:
        app.config['CHATBOT_VIGIA'] = True
        res = chatbot_vigia.avaliar(hist, conv_id='restrito',
                                   resultado_bot=_resultado())
        assert res['veredicto']['alerta'] is False
        assert 'conforme política restrita' in res['veredicto']['motivo']
        assert not chatbot_vigia._e_handoff_preguicoso_em_compra(
            hist, _resultado(), conv_id='restrito')
    modelo.assert_not_called()
    alerta.assert_not_called()


@pytest.mark.parametrize('mensagem', [
    'Meu pedido não chegou',
    'Meu croissant veio errado',
    'Quero reclamar, fiquei sem resposta de vocês',
])
@pytest.mark.parametrize('acao', ['handoff', 'handoff_repetido', 'silencio_humano'])
def test_reclamacao_restrita_alerta_equipe_sem_culpar_handoff(app, mensagem, acao):
    hist = [{'role': 'user', 'content': mensagem}]
    with app.app_context(), \
         patch.object(chatbot_vigia, '_chamar_modelo') as modelo, \
         patch.object(chatbot_vigia, '_numero_destino', return_value='equipe'), \
         patch('app.services.zapi.enviar_texto', return_value={'ok': True}) as alerta, \
         patch('app.services.chatwoot.enviar_mensagem') as cliente:
        app.config['CHATBOT_VIGIA'] = True
        res = chatbot_vigia.avaliar(hist, conv_id='reclamacao',
                                   resultado_bot=_resultado(acao))
        assert res['veredicto']['gravidade'] == 'alta'
        assert 'encaminhada corretamente' in res['veredicto']['motivo']
        assert res['enviado'] is True
    modelo.assert_not_called()
    cliente.assert_not_called()
    assert alerta.call_args[0][0] == 'equipe'


def test_marcador_persistido_exige_campo_interno_e_preserva_versao_antiga(app):
    from app.models import VigiaVeredito
    with app.app_context():
        casos = [
            ('novo', _resultado()),
            ('antigo', {'acao': 'handoff', 'tools_usadas': []}),
            ('sem_dado', {'acao': 'handoff'}),
            ('injetado', {'acao': 'handoff', 'tools_usadas': [
                chatbot_vigia.MARCADOR_POLITICA_RESTRITA]}),
        ]
        for cid, resultado in casos:
            chatbot_vigia._registrar({}, cid, '', 'quero comprar', resultado)
        rows = {v.conv_id: v for v in VigiaVeredito.query.all()}
        assert json.loads(rows['novo'].tools_usadas) == [
            chatbot_vigia.MARCADOR_POLITICA_RESTRITA]
        assert rows['antigo'].tools_usadas == '[]'
        assert rows['injetado'].tools_usadas == '[]'
        assert rows['sem_dado'].tools_usadas is None
        assert chatbot_auditor._eh_handoff_preguicoso(rows['novo']) is False
        assert chatbot_auditor._eh_handoff_preguicoso(rows['antigo']) is True
        assert chatbot_auditor._eh_handoff_preguicoso(rows['injetado']) is True


def test_supervisor_nao_inventa_encaminhamento_ausente(app):
    with app.app_context():
        app.config['CHATBOT_VIGIA'] = False
        res = chatbot_vigia._avaliar_interno(
            [{'role': 'user', 'content': 'Meu pedido não chegou'}],
            resultado_bot=_resultado('responder'))
        assert res['veredicto']['alerta'] is True
        assert 'não registrou encaminhamento' in res['veredicto']['motivo']


def test_auditor_separa_novos_encaminhamentos_do_historico(app):
    from app.models import VigiaVeredito
    from app.utils import agora
    with app.app_context():
        # Mensagem idêntica e mesma data: só o marcador muda a política.
        chatbot_vigia._registrar({}, 'nova', '', 'quero comprar', _resultado())
        chatbot_vigia._registrar({}, 'antiga', '', 'quero comprar',
                                 {'acao': 'handoff', 'tools_usadas': []})
        chatbot_vigia._registrar({}, 'faq', '', 'qual o endereço',
                                 _resultado('responder'))
        dados = chatbot_auditor._coletar_periodo(
            agora() - timedelta(hours=1), agora() + timedelta(hours=1))
        assert dados['conversas_unicas'] == 3
        assert dados['handoffs'] == 2
        assert dados['contencao_pct'] == 33.3  # série histórica preservada
        assert dados['handoffs_preguicosos'] == 1
        assert dados['eventos_restritos'] == 2
        assert dados['conversas_restritas'] == 2
        assert dados['handoffs_restritos'] == 1
        assert dados['eventos_sem_marcador_politica'] == 1
        assert VigiaVeredito.query.count() == 3
        politicas = {a['politica_atendimento'] for a in dados['amostras_handoff']}
        assert politicas == {None, chatbot_vigia.POLITICA_ATENDIMENTO_RESTRITA}
        texto = chatbot_auditor._linha_contencao(dados)
        assert '1 encaminhamento(s) correto(s)' in texto
        assert 'Contenção' not in texto
        assert 'preguiçoso' not in texto
        comparativo = chatbot_auditor._resumo_comparativo(dados)
        assert comparativo['handoffs_restritos'] == 1


def test_marcador_em_outro_turno_nao_justifica_handoff_historico(app):
    with app.app_context():
        chatbot_vigia._registrar({}, 'mesma', '', 'quero comprar', _resultado())
        assert chatbot_vigia.handoff_foi_preguicoso([], conv_id='mesma') is True


def test_prompts_nao_pressionam_meta_de_contencao_ou_autonomia():
    for prompt in (chatbot_auditor.PROMPT_AUDITOR,
                   chatbot_auditor.PROMPT_AUDITOR_RESUMO):
        assert '90%' not in prompt
        assert '24/09/2026' in prompt
        assert 'handoffs_restritos' in prompt

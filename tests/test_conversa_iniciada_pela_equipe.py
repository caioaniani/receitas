"""Conversa INICIADA PELA EQUIPE ("Chamar cliente") em que o cliente nunca
escreveu não é conversa do bot nem espera de atendimento (22/09/2026).

Caso real, conv 2429: a equipe clicou "Chamar" (template) às 10:15; a
conversa nasceu na fila do bot (inbox com Agent Bot cria em `pending`), o
bot mandou follow-up às 10:25, o vigia deu "[ABANDONO 17min]" (ALTA +
WhatsApp) às 10:45 e a espera-humana cobrou "Urgente" no painel e no
WhatsApp a cada 15-20 min até o meio-dia — e quando a equipe abria a
conversa, "não tinha conversa nenhuma" (só os nossos templates).
"""
from datetime import timedelta
from unittest.mock import patch

from app.extensions import db
from app.models import EsperaAtendimento, VigiaVeredito
from app.utils import agora

_SO_NOSSAS = [
    {'role': 'assistant', 'humano': True,
     'content': 'Olá Cintia, aqui é da O Pão. Ficamos com uma dúvida sobre o seu pedido E3862E49.'},
    {'role': 'assistant', 'humano': False,
     'content': 'Oi, Cintia! Ainda ficou aquela dúvida sobre o pedido, você consegue nos ajudar?'},
]


def _seed_2429(estado='aguardando', resolvido_em=None):
    inicio = agora() - timedelta(minutes=78)
    db.session.add(EsperaAtendimento(
        conversa_id='2429', inicio_em=inicio, nome='Cintia Tuyama',
        mensagem='[ABANDONO 17min] ', grave=True, estado=estado,
        resolvido_em=resolvido_em))
    db.session.add(VigiaVeredito(
        criado_em=inicio, conv_id='2429', cliente='Cintia Tuyama',
        mensagem_cliente='[ABANDONO 17min] ', bot_acao=None, alerta=True,
        gravidade='alta', motivo_vigia='[17 min sem resposta] cliente sumiu'))
    db.session.commit()


def test_cliente_ja_falou_e_fonte_unica():
    from app.services.chatbot import cliente_ja_falou
    assert cliente_ja_falou(_SO_NOSSAS) is False
    assert cliente_ja_falou([]) is False
    assert cliente_ja_falou(None) is False
    assert cliente_ja_falou(_SO_NOSSAS + [{'role': 'user', 'content': 'Oi'}]) is True


def test_abandono_nao_avalia_conversa_sem_fala_do_cliente(app):
    from app.services import chatbot_vigia
    with app.app_context(), \
         patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'x'}), \
         patch('app.services.chatbot_vigia._chamar_modelo_abandono') as modelo, \
         patch('app.services.zapi.enviar_texto') as zapi:
        app.config['CHATBOT_VIGIA'] = '1'
        res = chatbot_vigia.avaliar_abandono(
            _SO_NOSSAS, conv_id=2429, nome_contato='Cintia Tuyama',
            minutos_sem_resposta=17)
    assert 'equipe' in res['pulou']
    modelo.assert_not_called()
    zapi.assert_not_called()
    with app.app_context():
        assert VigiaVeredito.query.filter(VigiaVeredito.conv_id == '2429').count() == 0


def test_followup_nao_cutuca_conversa_sem_fala_do_cliente(app):
    from app.services import chatbot
    with app.app_context(), \
         patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'x'}), \
         patch('app.services.chatwoot.listar_conversas_paradas',
               return_value=[{'id': 2429, 'nome_contato': 'Cintia Tuyama',
                              'minutos_paradas': 9}]), \
         patch('app.services.chatwoot.buscar_historico',
               return_value=list(_SO_NOSSAS[:1])), \
         patch('app.services.chatbot._followup_gerar_texto') as gerar, \
         patch('app.services.chatwoot.enviar_mensagem') as envia:
        r = chatbot.followup_conversas_paradas()
    assert r == {'avaliadas': 0, 'enviadas': 0}
    gerar.assert_not_called()
    envia.assert_not_called()


def test_preparar_conversa_sem_cliente_sai_do_painel_e_da_cobranca(app):
    from app.services import atendimento_pendente
    conversa = {'id': 2429, 'nome_contato': 'Cintia Tuyama', 'minutos_paradas': 78,
                'status': 'open', 'telefone': '5514999999999'}
    with app.app_context():
        _seed_2429()
        # Antes: a linha aguardando + ALTA de abandono aparece no painel.
        assert [a for a in atendimento_pendente.alertas_painel() if a['conv_id'] == '2429']
        assert atendimento_pendente.preparar(conversa, list(_SO_NOSSAS)) is None
        row = db.session.get(EsperaAtendimento, '2429')
        assert row.estado == 'sem_cliente'
        assert row.resolvido_em is not None and row.proximo_aviso_em is None
        # Depois: some do painel (mesmo com o ALTA recente no banco)...
        assert not [a for a in atendimento_pendente.alertas_painel() if a['conv_id'] == '2429']
        # ...e da lista de candidatas da cobrança ao dono.
        with patch('app.services.chatwoot.listar_conversas_paradas', return_value=[]), \
             patch('app.services.chatwoot.consultar_conversa') as consulta:
            assert atendimento_pendente.candidatos() == []
            consulta.assert_not_called()


def test_preparar_cliente_que_responde_depois_volta_a_aguardar_sem_gravidade_fantasma(app):
    import time

    from app.services import atendimento_pendente
    conversa = {'id': 2429, 'nome_contato': 'Cintia Tuyama', 'minutos_paradas': 20}
    with app.app_context():
        # Marcada `sem_cliente` num ciclo anterior (10 min atrás); segue assim
        # enquanto só houver mensagens nossas.
        _seed_2429(estado='sem_cliente', resolvido_em=agora() - timedelta(minutes=10))
        assert atendimento_pendente.preparar(conversa, list(_SO_NOSSAS)) is None
        assert db.session.get(EsperaAtendimento, '2429').estado == 'sem_cliente'
        # A cliente respondeu há 30 s: volta a ser espera normal.
        historico = list(_SO_NOSSAS) + [{'role': 'user', 'content': 'Oi, pode entregar de novo?',
                                         'humano': False, 'created_at': time.time() - 30}]
        row = atendimento_pendente.preparar(conversa, historico, min_minutos=0)
        assert row is not None and row.estado == 'aguardando'
        # O ALTA de "abandono" da fase sem cliente não vira "Urgente" agora.
        assert row.grave is False
        assert row.inicio_em > row.resolvido_em


def test_resposta_da_equipe_no_painel_nao_reabre_sem_cliente(app):
    from app.services import atendimento_pendente
    conversa = {'id': 2429, 'nome_contato': 'Cintia Tuyama', 'minutos_paradas': 78}
    with app.app_context():
        _seed_2429()
        atendimento_pendente.preparar(conversa, list(_SO_NOSSAS))
        # Alguém digita "ola" na conversa pelo painel (caso real, 12:04).
        atendimento_pendente.confirmar_acao_painel(
            '2429', 'responder', iniciado_em=agora(), usuario_id=1)
        assert db.session.get(EsperaAtendimento, '2429').estado == 'sem_cliente'
        assert not [a for a in atendimento_pendente.alertas_painel() if a['conv_id'] == '2429']
        # Resolver continua resolvendo.
        atendimento_pendente.confirmar_acao_painel(
            '2429', 'resolved', iniciado_em=agora(), usuario_id=1)
        assert db.session.get(EsperaAtendimento, '2429').estado == 'resolvido'


def test_chamar_cliente_tira_a_conversa_da_fila_do_bot(app):
    """A conversa criada pelo "Chamar" nasce `pending` (inbox com Agent Bot);
    o serviço a passa para `open` logo após o template — a resposta do
    cliente cai na fila da equipe, não no bot."""
    from test_chamar_cliente_whatsapp import _cfg_whatsapp, _resp

    from app.services import chatwoot
    with app.app_context():
        _cfg_whatsapp(app)

        def fake_get(url, **kw):
            if '/contacts/search' in url:
                return _resp(200, {'payload': []})
            if url.endswith('/conversations'):
                return _resp(200, {'payload': []})
            return _resp(404, {})

        def fake_post(url, json=None, **kw):
            if url.endswith('/contacts'):
                return _resp(200, {'payload': {'contact': {
                    'id': 55,
                    'contact_inboxes': [{'inbox': {'id': 7}, 'source_id': 'src-7'}],
                }}})
            if url.endswith('/conversations'):
                return _resp(200, {'id': 900})
            if '/messages' in url:
                return _resp(200, {'id': 1})
            return _resp(404, {})

        with patch.object(chatwoot.requests, 'get', side_effect=fake_get), \
                patch.object(chatwoot.requests, 'post', side_effect=fake_post), \
                patch('app.services.chatwoot.definir_status',
                      return_value={'ok': True}) as status:
            res = chatwoot.iniciar_conversa_whatsapp(
                '11999998888', 'Simone', params=['Simone', 'ABC123'])
        assert res['ok'] is True and res['aberta'] is True
        assert status.call_args.args[:2] == (900, 'open')
        # Falha no toggle não derruba o envio (o template já saiu).
        with patch.object(chatwoot.requests, 'get', side_effect=fake_get), \
                patch.object(chatwoot.requests, 'post', side_effect=fake_post), \
                patch('app.services.chatwoot.definir_status',
                      return_value={'ok': False, 'erro': 'HTTP 500'}):
            res = chatwoot.iniciar_conversa_whatsapp(
                '11999998888', 'Simone', params=['Simone', 'ABC123'])
        assert res['ok'] is True and res['aberta'] is False

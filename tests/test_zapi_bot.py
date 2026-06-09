"""Smoke tests do bot WhatsApp privado do dono (read-only via Z-API)."""
from unittest.mock import patch

import pytest


@pytest.fixture
def app_zapi(app):
    """App com configs do bot setadas + 1 Usuario owner ja criado.

    Nao usa o admin auto-criado pelo create_app() porque em teste a tabela
    nao existe quando ele tenta — entao criamos manualmente aqui."""
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        app.config['ZAPI_BOT_DONO_NUMERO'] = '5511988888888'
        app.config['ZAPI_BOT_WEBHOOK_TOKEN'] = 'secret-token'
        app.config['ZAPI_NUMERO_DESTINO'] = '5511988888888'
        app.config['ANTHROPIC_API_KEY'] = 'fake-key'
        u = Usuario(nome='Dono', login='dono', papel='admin',
                    is_owner=True)
        u.set_senha('xxxxxxxxxx')
        db.session.add(u)
        db.session.commit()
        yield app


def test_webhook_rejeita_token_invalido(app_zapi):
    c = app_zapi.test_client()
    r = c.post('/zapi/webhook?k=outro', json={'phone': '5511988888888'})
    assert r.status_code == 403


def test_webhook_aceita_token_e_responde_200(app_zapi):
    """Bot recebe payload valido — webhook deve responder 200 imediato
    (processamento eh async). Whitelist sera checada no processamento."""
    c = app_zapi.test_client()
    with patch('app.services.zapi_bot.processar_payload') as p:
        r = c.post('/zapi/webhook?k=secret-token',
                    json={'phone': '5511988888888', 'messageId': 'X1',
                          'text': {'message': 'oi'}})
    assert r.status_code == 200
    # processamento dispara em thread — pode nao ter rodado ainda; importa
    # o webhook nao bloquear


def test_payload_de_outro_numero_eh_ignorado(app_zapi):
    """Numero != ZAPI_BOT_DONO_NUMERO nunca chega no copilot."""
    from app.services import zapi_bot
    with app_zapi.app_context():
        with patch('app.services.copilot.interpretar') as itr:
            zapi_bot.processar_payload({
                'phone': '5511777777777',  # nao eh o dono
                'messageId': 'NN',
                'text': {'message': 'oi'},
            })
    itr.assert_not_called()


def test_payload_do_dono_chama_copilot_apenas_leitura(app_zapi):
    """E enche o historico e responde via Z-API."""
    from app.services import zapi_bot
    with app_zapi.app_context():
        with patch('app.services.copilot.interpretar',
                   return_value={'tipo': 'conversa',
                                 'explicacao': 'Tudo bem, e voce?'}) as itr, \
             patch('app.services.zapi.enviar_texto') as send:
            zapi_bot.processar_payload({
                'phone': '5511988888888',
                'messageId': 'D1',
                'text': {'message': 'oi tudo bem?'},
            })
        itr.assert_called_once()
        # Crucial: copilot foi chamado em modo leitura
        assert itr.call_args.kwargs.get('apenas_leitura') is True
        send.assert_called_once()
        # Texto enviado ao WhatsApp = explicacao do Claude
        assert send.call_args[0][1] == 'Tudo bem, e voce?'


def test_idempotencia_message_id(app_zapi):
    """Z-API pode reenviar webhook — mesmo messageId processa 1 vez so."""
    from app.services import zapi_bot
    with app_zapi.app_context():
        with patch('app.services.copilot.interpretar',
                   return_value={'tipo': 'conversa', 'explicacao': 'ok'}) as itr, \
             patch('app.services.zapi.enviar_texto'):
            payload = {'phone': '5511988888888', 'messageId': 'DUP1',
                       'text': {'message': 'a'}}
            zapi_bot.processar_payload(payload)
            zapi_bot.processar_payload(payload)
        assert itr.call_count == 1   # so a 1a vez


def test_historico_persiste_entre_turnos(app_zapi):
    """Cada mensagem adiciona ao historico — proximo turno ve o anterior."""
    import json

    from app.models import ZapiBotConversa
    from app.services import zapi_bot
    with app_zapi.app_context():
        with patch('app.services.copilot.interpretar',
                   return_value={'tipo': 'conversa', 'explicacao': 'resposta'}), \
             patch('app.services.zapi.enviar_texto'):
            zapi_bot.processar_payload({
                'phone': '5511988888888', 'messageId': 'H1',
                'text': {'message': 'primeira'},
            })
            zapi_bot.processar_payload({
                'phone': '5511988888888', 'messageId': 'H2',
                'text': {'message': 'segunda'},
            })
        conv = ZapiBotConversa.query.first()
        hist = json.loads(conv.mensagens_json)
        assert len(hist) == 4   # 2 user + 2 assistant
        assert hist[0]['content'] == 'primeira'
        assert hist[2]['content'] == 'segunda'

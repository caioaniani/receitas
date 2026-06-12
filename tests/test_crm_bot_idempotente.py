"""Idempotencia do webhook /crm/bot do Chatwoot.

Bug em risco real: Chatwoot reenvia `message_created` se o webhook
demora 5s+ (bot precisa de Claude + tools, passa de 5s as vezes). Sem
dedupe, a mesma mensagem do cliente vira 2 turnos do bot — resposta
duplicada no canal e gasto dobrado de token. Dedupe via tabela
ChatwootEventoProcessado, PK = message id."""
from unittest.mock import patch


def _payload(msg_id=42, conv_id=170, content='oi'):
    return {
        'event': 'message_created',
        'id': msg_id,
        'message_type': 'incoming',
        'content': content,
        'conversation': {'id': conv_id, 'status': 'pending',
                          'meta': {'sender': {'name': 'Cliente Teste'}}},
        'sender': {'name': 'Cliente Teste'},
    }


def test_webhook_aceita_1a_vez_e_recusa_replay(app):
    app.config['CHATWOOT_BOT_SECRET'] = 'segredo-teste'
    c = app.test_client()
    with patch('threading.Thread') as fake_thread:
        r1 = c.post('/crm/bot?k=segredo-teste', json=_payload(msg_id=100))
        assert r1.status_code == 200
        assert r1.get_json().get('ignorado') != 'duplicado'
        # Replay com MESMO message_id (Chatwoot retransmitiu por timeout)
        r2 = c.post('/crm/bot?k=segredo-teste', json=_payload(msg_id=100))
        assert r2.status_code == 200
        assert r2.get_json()['ignorado'] == 'duplicado'
        # E o processamento async so foi disparado UMA vez
        assert fake_thread.call_count == 1


def test_mensagens_distintas_processam_independentes(app):
    app.config['CHATWOOT_BOT_SECRET'] = 'segredo-teste'
    c = app.test_client()
    with patch('threading.Thread') as fake_thread:
        c.post('/crm/bot?k=segredo-teste', json=_payload(msg_id=201))
        c.post('/crm/bot?k=segredo-teste', json=_payload(msg_id=202))
        c.post('/crm/bot?k=segredo-teste', json=_payload(msg_id=203))
    assert fake_thread.call_count == 3


def test_payload_sem_message_id_segue_o_fluxo_antigo(app):
    """Defensivo: se o Chatwoot mudar o shape do payload e nao mandar
    `id`/`message_id`, o webhook nao pode quebrar — segue sem dedupe
    (volta ao comportamento anterior). Pior caso: replay vira 2 turnos
    (estado atual antes do fix), nao 500 no servidor."""
    app.config['CHATWOOT_BOT_SECRET'] = 'segredo-teste'
    c = app.test_client()
    payload = _payload()
    payload.pop('id')
    with patch('threading.Thread'):
        r = c.post('/crm/bot?k=segredo-teste', json=payload)
    assert r.status_code == 200


def test_replay_persiste_apos_ack_do_primeiro(app):
    """Mesmo depois do thread async terminar (registro ja salvo), o
    replay continua sendo rejeitado — protege contra Chatwoot retentar
    apos 10s, 30s, 1h."""
    from app.extensions import db
    from app.models import ChatwootEventoProcessado
    app.config['CHATWOOT_BOT_SECRET'] = 'segredo-teste'
    c = app.test_client()
    with patch('threading.Thread'):
        c.post('/crm/bot?k=segredo-teste', json=_payload(msg_id=999))
    # Confirma que ficou gravado
    row = db.session.get(ChatwootEventoProcessado, '999')
    assert row is not None
    assert row.conversation_id == '170'
    # Replay simulado depois — segue rejeitando
    with patch('threading.Thread') as fake_thread:
        r = c.post('/crm/bot?k=segredo-teste', json=_payload(msg_id=999))
        assert r.get_json()['ignorado'] == 'duplicado'
        fake_thread.assert_not_called()

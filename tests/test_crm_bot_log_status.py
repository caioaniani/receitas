"""Log de status quando o webhook /crm/bot ignora.

Bug real (12/06/2026, conv #198): cliente mandou 'Olá' 46min depois e
o bot nao respondeu. Hipotese: o webhook chegou em status diferente de
'pending' (a conversa estava 'open' apos handoff) e foi ignorada em
silencio. Sem log do status real, e impossivel distinguir 'config do
Chatwoot precisa reabrir como pending' de 'bug nosso'."""
from unittest.mock import patch


def test_resposta_inclui_status_quando_ignora(app):
    """Webhook responde 200 com o status real que veio — debugar fica
    trivial (basta olhar o JSON da resposta no painel de webhooks do
    Chatwoot ou nos logs do Railway)."""
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    c = app.test_client()
    payload = {
        'event': 'message_created',
        'id': 1001,
        'message_type': 'incoming',
        'content': 'Olá',
        'conversation': {'id': 198, 'status': 'open'},
    }
    r = c.post('/crm/bot?k=seg', json=payload)
    assert r.status_code == 200
    body = r.get_json()
    assert body['ignorado'] == 'nao-pending'
    assert body['status'] == 'open'   # informacao chave pra diagnostico


def test_log_inclui_status_e_conv_id(app):
    """O log do Railway tem o status — proxima vez que cliente reclamar
    'bot nao respondeu', basta grep no log: 'crm/bot ignora: status=X'."""
    app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    c = app.test_client()
    payload = {
        'event': 'message_created',
        'id': 1002,
        'message_type': 'incoming',
        'content': 'Olá',
        'conversation': {'id': 198, 'status': 'resolved'},
    }
    with patch('app.blueprints.crm.routes.logger') as fake_log:
        c.post('/crm/bot?k=seg', json=payload)
    # logger.info foi chamado com status='resolved' e conv=198
    chamadas = [str(c) for c in fake_log.info.call_args_list]
    assert any('resolved' in c and '198' in c for c in chamadas), \
        f'log de diagnostico nao saiu: {chamadas}'

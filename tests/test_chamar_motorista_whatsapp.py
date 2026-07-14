"""Botão "Chamar motoboy" (Lalamove) do painel de entregas — 14/07/2026.

A Lalamove não expõe o chat do app deles; a conversa nasce no NOSSO
WhatsApp/Chatwoot com o telefone do motorista (que o webhook DRIVER_ASSIGNED
já grava em LalamoveEntrega.motorista_*) e abre no painel da direita.

Espelha o padrão de tests/test_chamar_cliente_whatsapp.py (Chatwoot sempre
mockado — teste nunca sai pra internet).
"""
from unittest.mock import patch

from app.extensions import db
from app.models import LalamoveEntrega


def _login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def _corrida(codigo='PED42', nome='Carlos', fone='11988887777',
             status='ON_GOING'):
    e = LalamoveEntrega(pedido_code=codigo, order_id=f'ord-{codigo}',
                        status=status, motorista_nome=nome,
                        motorista_telefone=fone)
    db.session.add(e)
    db.session.commit()
    return e


def test_endpoint_chama_motorista_e_devolve_conv_id(app, admin_user):
    with app.app_context():
        e = _corrida()
        eid = e.id
    client = app.test_client()
    _login(client, admin_user)
    with patch('app.services.chatwoot.iniciar_conversa_whatsapp',
               return_value={'ok': True, 'conversation_id': 901,
                             'nova': True, 'erro': None}) as m:
        r = client.post('/entregas/api/atendimento/chamar-motorista',
                        json={'entrega_id': eid})
    assert r.status_code == 200
    d = r.get_json()
    assert d['ok'] is True and d['conversation_id'] == 901
    assert d['nome'] == 'Motoboy Carlos'
    args, kwargs = m.call_args
    assert args[0] == '11988887777' and args[1] == 'Carlos'
    assert kwargs['params'] == ['Carlos', 'PED42']
    # Sem template dedicado configurado → None (serviço usa o padrão).
    assert kwargs['template_nome'] is None
    assert kwargs['template_corpo'] is None


def test_endpoint_usa_template_motoboy_quando_configurado(app, admin_user):
    with app.app_context():
        e = _corrida('PED43')
        eid = e.id
    app.config['CHATWOOT_WHATSAPP_TEMPLATE_MOTOBOY'] = 'entrega_motoboy'
    app.config['CHATWOOT_WHATSAPP_TEMPLATE_MOTOBOY_CORPO'] = (
        'Oi {{1}}, sou da padaria O Pão, sobre a entrega {{2}}.')
    client = app.test_client()
    _login(client, admin_user)
    with patch('app.services.chatwoot.iniciar_conversa_whatsapp',
               return_value={'ok': True, 'conversation_id': 902,
                             'nova': False, 'erro': None}) as m:
        r = client.post('/entregas/api/atendimento/chamar-motorista',
                        json={'entrega_id': eid})
    assert r.status_code == 200
    _, kwargs = m.call_args
    assert kwargs['template_nome'] == 'entrega_motoboy'
    assert 'padaria' in kwargs['template_corpo']


def test_endpoint_corrida_inexistente_404_e_sem_id_400(app, admin_user):
    client = app.test_client()
    _login(client, admin_user)
    r = client.post('/entregas/api/atendimento/chamar-motorista',
                    json={'entrega_id': 999999})
    assert r.status_code == 404
    r = client.post('/entregas/api/atendimento/chamar-motorista', json={})
    assert r.status_code == 400


def test_endpoint_sem_motorista_400(app, admin_user):
    """Corrida ainda em ASSIGNING_DRIVER (webhook não trouxe o motorista):
    recusa com mensagem clara em vez de mandar template pra ninguém."""
    with app.app_context():
        e = _corrida('PED44', nome=None, fone='',
                     status='ASSIGNING_DRIVER')
        eid = e.id
    client = app.test_client()
    _login(client, admin_user)
    r = client.post('/entregas/api/atendimento/chamar-motorista',
                    json={'entrega_id': eid})
    assert r.status_code == 400
    assert 'motorista' in r.get_json()['erro'].lower()


def test_endpoint_falha_do_servico_vira_502_com_conv_id(app, admin_user):
    with app.app_context():
        e = _corrida('PED45')
        eid = e.id
    client = app.test_client()
    _login(client, admin_user)
    with patch('app.services.chatwoot.iniciar_conversa_whatsapp',
               return_value={'ok': False, 'conversation_id': 903,
                             'nova': True, 'erro': 'HTTP 422: template'}):
        r = client.post('/entregas/api/atendimento/chamar-motorista',
                        json={'entrega_id': eid})
    assert r.status_code == 502
    d = r.get_json()
    assert d['ok'] is False and d['conversation_id'] == 903


def test_servico_usa_template_override_no_envio(app):
    """iniciar_conversa_whatsapp com template_nome/corpo custom manda o
    template CERTO pro Chatwoot (nome no payload, corpo renderizado)."""
    from app.services import chatwoot
    posts = []

    def fake_get(url, **kw):
        from unittest.mock import MagicMock
        r = MagicMock()
        r.status_code = 200
        if '/contacts/search' in url:
            r.json.return_value = {'payload': [
                {'id': 7, 'contact_inboxes': [
                    {'inbox': {'id': 7}, 'source_id': 'src-1'}]}]}
        else:  # conversas do contato
            r.json.return_value = {'payload': [
                {'id': 55, 'status': 'open', 'inbox_id': 7,
                 'last_activity_at': 1}]}
        return r

    def fake_post(url, **kw):
        from unittest.mock import MagicMock
        posts.append((url, kw.get('json')))
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {'id': 55}
        return r

    with app.app_context():
        app.config.update(CHATWOOT_URL='https://cw.x', CHATWOOT_API_TOKEN='t',
                          CHATWOOT_ACCOUNT_ID='1',
                          CHATWOOT_WHATSAPP_INBOX_ID='7',
                          CHATWOOT_WHATSAPP_TEMPLATE='duvida_pedido')
        with patch.object(chatwoot.requests, 'get', side_effect=fake_get), \
                patch.object(chatwoot.requests, 'post', side_effect=fake_post):
            res = chatwoot.iniciar_conversa_whatsapp(
                '11988887777', 'Carlos', params=['Carlos', 'PED42'],
                template_nome='entrega_motoboy',
                template_corpo='Oi {{1}}, entrega {{2}}.')
    assert res['ok'] is True and res['conversation_id'] == 55
    msg = next(j for u, j in posts if u.endswith('/messages'))
    assert msg['template_params']['name'] == 'entrega_motoboy'
    assert msg['content'] == 'Oi Carlos, entrega PED42.'

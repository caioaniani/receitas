"""Regressoes da Fase 0: Z-API nunca pode confirmar envio no escuro."""
from unittest.mock import patch


class _Resposta:
    def __init__(self, status=200, corpo=None, texto=None,
                 content_type='application/json'):
        self.status_code = status
        self._corpo = corpo
        self.text = texto if texto is not None else ('' if corpo is None else 'json')
        self.content = b''
        self.headers = {'Content-Type': content_type}

    def json(self):
        if isinstance(self._corpo, Exception):
            raise self._corpo
        return self._corpo


def _configurar(app):
    app.config.update(
        ZAPI_INSTANCE_ID='instancia-1',
        ZAPI_TOKEN='token-1',
        ZAPI_CLIENT_TOKEN='client-1',
        ZAPI_NUMEROS_PERMITIDOS='5511999999999',
        ZAPI_THROTTLE='0',
    )


def _login(client):
    client.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def test_http_200_sem_id_e_falha_real(app):
    from app.services import zapi
    _configurar(app)
    resposta = _Resposta(corpo={'error': 'Instance is not connected'})
    with patch.object(zapi, 'status_instancia', return_value={
            'ok': True, 'conectado': True, 'detalhe': 'conectado'}), \
         patch.object(zapi.requests, 'post', return_value=resposta):
        resultado = zapi.enviar_texto('5511999999999', 'teste')
    assert resultado['ok'] is False
    assert 'not connected' in resultado['erro']


def test_sucesso_exige_e_devolve_zaap_id(app):
    from app.services import zapi
    _configurar(app)
    resposta = _Resposta(corpo={'zaapId': 'zaap-123', 'messageId': 'msg-456'})
    with patch.object(zapi, 'status_instancia', return_value={
            'ok': True, 'conectado': True, 'detalhe': 'conectado'}), \
         patch.object(zapi.requests, 'post', return_value=resposta):
        resultado = zapi.enviar_texto('5511999999999', 'teste')
    assert resultado == {
        'ok': True,
        'zaap_id': 'zaap-123',
        'message_id': 'msg-456',
        'response': {'zaapId': 'zaap-123', 'messageId': 'msg-456'},
    }


def test_message_id_sozinho_tambem_confirma(app):
    from app.services import zapi
    _configurar(app)
    resposta = _Resposta(corpo={'messageId': 'msg-456'})
    with patch.object(zapi, 'status_instancia', return_value={
            'ok': True, 'conectado': True, 'detalhe': 'conectado'}), \
         patch.object(zapi.requests, 'post', return_value=resposta):
        resultado = zapi.enviar_texto('5511999999999', 'teste')
    assert resultado['ok'] is True
    assert resultado['zaap_id'] == 'msg-456'


def test_desconectado_bloqueia_antes_do_post(app):
    from app.services import zapi
    _configurar(app)
    with patch.object(zapi, 'status_instancia', return_value={
            'ok': True, 'conectado': False, 'detalhe': 'desconectado'}), \
         patch.object(zapi.requests, 'post') as enviar:
        resultado = zapi.enviar_texto('5511999999999', 'teste')
    assert resultado['ok'] is False
    assert resultado['desconectado'] is True
    enviar.assert_not_called()


def test_proxies_externos_mandam_client_token(app):
    from app.services import zapi
    _configurar(app)
    qr_resp = _Resposta(corpo={'value': 'QUJD'})
    restart_resp = _Resposta(corpo={'value': True})
    assinatura_resp = _Resposta(corpo={'value': True})
    with patch.object(zapi.requests, 'get', side_effect=[
            qr_resp, restart_resp]) as get, \
         patch.object(zapi.requests, 'put', return_value=assinatura_resp) as put:
        assert zapi.obter_qr_code()['imagem'] == 'data:image/png;base64,QUJD'
        assert zapi.reiniciar_instancia()['ok'] is True
        assinatura = zapi.assinar_webhooks_conexao(
            'https://gestao.exemplo.com', 'segredo')
    assert assinatura['ok'] is True
    assert get.call_count == 2
    assert all(c.kwargs['headers']['Client-Token'] == 'client-1'
               for c in get.call_args_list)
    assert put.call_count == 2
    assert all(c.kwargs['headers']['Client-Token'] == 'client-1'
               for c in put.call_args_list)
    callbacks = [c.kwargs['json']['value'] for c in put.call_args_list]
    assert callbacks == [
        'https://gestao.exemplo.com/notificacoes/webhook/zapi/conectado?k=segredo',
        'https://gestao.exemplo.com/notificacoes/webhook/zapi/desconectado?k=segredo',
    ]


def test_historico_guarda_id_e_erro_real(app):
    from app.models import NotificacaoWhatsapp
    from app.services import whatsapp
    with patch('app.services.zapi.enviar_texto', return_value={
            'ok': True, 'zaap_id': 'zaap-123'}):
        whatsapp.notificar('5511999999999', 'entregue', 'manual')
    with patch('app.services.zapi.enviar_texto', return_value={
            'ok': False, 'erro': 'Instance is not connected'}):
        whatsapp.notificar('5511999999999', 'falhou', 'manual')
    registros = NotificacaoWhatsapp.query.order_by(
        NotificacaoWhatsapp.id).all()
    assert registros[0].ok is True
    assert registros[0].zaap_id == 'zaap-123'
    assert registros[0].erro in (None, '')
    assert registros[1].ok is False
    assert registros[1].zaap_id in (None, '')
    assert registros[1].erro == 'Instance is not connected'


def test_historico_recusa_ok_sem_identificador(app):
    from app.models import NotificacaoWhatsapp
    from app.services import whatsapp
    with patch('app.services.zapi.enviar_texto', return_value={'ok': True}):
        resultado = whatsapp.notificar('5511999999999', 'cego', 'manual')
    registro = NotificacaoWhatsapp.query.one()
    assert resultado['ok'] is False
    assert registro.ok is False
    assert 'sem zaapId/messageId' in registro.erro


def test_proxies_sao_admin_e_usam_servico(app, admin_user):
    from app.services import zapi
    client = app.test_client()
    assert client.get('/notificacoes/status.json').status_code in (302, 401)
    _login(client)
    with patch.object(zapi, 'status_instancia', return_value={
            'ok': True, 'conectado': True, 'detalhe': 'conectado'}):
        status = client.get('/notificacoes/status.json')
    assert status.status_code == 200
    assert status.get_json()['conectado'] is True

    with patch.object(zapi, 'obter_qr_code', return_value={
            'ok': True, 'imagem': 'data:image/png;base64,QUJD'}):
        qr = client.get('/notificacoes/qr')
    assert qr.status_code == 200
    assert b'data:image/png;base64,QUJD' in qr.data

    with patch.object(zapi, 'reiniciar_instancia', return_value={
            'ok': True}) as reiniciar:
        resposta = client.post('/notificacoes/reiniciar')
    assert resposta.status_code == 302
    reiniciar.assert_called_once_with()


def test_webhook_desconexao_alerta_uma_vez_e_conexao_fecha(app):
    from app.models import AppConfig
    from app.services import zapi_saude
    _configurar(app)
    payload_off = {
        'type': 'DisconnectedCallback', 'disconnected': True,
        'instanceId': 'instancia-1', 'error': 'Device has been disconnected',
    }
    payload_on = {
        'type': 'ConnectedCallback', 'connected': True,
        'instanceId': 'instancia-1', 'phone': '5511999881605',
    }
    with patch.object(zapi_saude, '_alertar_email') as email, \
         patch.object(zapi_saude, '_alertar_sentry') as sentry:
        assert zapi_saude.registrar_desconexao(payload_off)['alertou'] is True
        assert zapi_saude.registrar_desconexao(payload_off)['alertou'] is False
        assert email.call_count == 1
        assert sentry.call_count == 1
        assert zapi_saude.registrar_conexao(payload_on)['normalizou'] is True
    assert AppConfig.get('zapi_conexao_estado') == 'conectado'


def test_webhook_recusa_token_invalido(app):
    from app.services import zapi_saude
    _configurar(app)
    client = app.test_client()
    resposta = client.post(
        '/notificacoes/webhook/zapi/desconectado?k=errado',
        json={'instanceId': 'instancia-1', 'disconnected': True})
    assert resposta.status_code == 403
    token = zapi_saude.webhook_token()
    with patch.object(zapi_saude, 'registrar_desconexao',
                      return_value={'ok': True}) as registrar:
        resposta = client.post(
            f'/notificacoes/webhook/zapi/desconectado?k={token}',
            json={'instanceId': 'instancia-1', 'disconnected': True})
    assert resposta.status_code == 200
    registrar.assert_called_once()

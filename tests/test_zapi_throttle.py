"""Teto GLOBAL de envio/hora do Z-API (anti-spam do WhatsApp, 14/07/2026).

Incidente: o WhatsApp restringiu o número por volume de mensagem automática.
`zapi.enviar_texto` é o ponto único por onde TUDO passa — ganhou um teto/hora
com kill-switch. Garantias travadas aqui:
- acima do teto, msg NÃO-crítica é SEGURADA (não vai pro WhatsApp);
- msg CRÍTICA (Lalamove/pedido pago) NUNCA é segurada;
- o que foi segurado NÃO some: vira UM digest ao dono no próximo envio liberado;
- kill-switch ZAPI_THROTTLE=0 desliga tudo.

O teto é OFF por padrão sob TESTING (estado global de módulo vazaria entre os
testes que compartilham o app); estes setam ZAPI_THROTTLE='1' de propósito e
zeram o estado antes de cada caso.
"""
import time
from unittest.mock import patch

import pytest


class _Resp:
    status_code = 200
    text = '{"zaapId": "zaap-1", "messageId": "msg-1"}'

    @staticmethod
    def json():
        return {'zaapId': 'zaap-1', 'messageId': 'msg-1'}


def _cfg(app, **extra):
    app.config['ZAPI_INSTANCE_ID'] = 'inst1'
    app.config['ZAPI_TOKEN'] = 'tok1'
    app.config['ZAPI_CLIENT_TOKEN'] = ''
    app.config['ZAPI_NUMEROS_PERMITIDOS'] = '5511999999999'
    app.config['ZAPI_NUMERO_DESTINO'] = '5511999999999'
    app.config['ZAPI_THROTTLE'] = '1'
    app.config['ZAPI_MAX_HORA'] = 3
    for k, v in extra.items():
        app.config[k] = v


@pytest.fixture(autouse=True)
def _reset_throttle(monkeypatch):
    """Zera o estado global do teto antes de cada teste deste módulo."""
    from app.services import zapi
    monkeypatch.setattr(zapi, 'status_instancia', lambda: {
        'ok': True, 'conectado': True, 'detalhe': 'conectado'})
    zapi._env_ts.clear()
    zapi._seg_previews.clear()
    zapi._seg_n = 0
    yield
    zapi._env_ts.clear()
    zapi._seg_previews.clear()
    zapi._seg_n = 0


def test_sob_teto_envia_normal(app):
    from app.services import zapi
    _cfg(app)
    with app.app_context(), \
         patch('app.services.zapi.requests.post', return_value=_Resp()) as post:
        for _ in range(3):
            r = zapi.enviar_texto('5511999999999', 'alerta')
            assert r['ok'] is True
    assert post.call_count == 3


def test_acima_do_teto_segura_sem_postar(app):
    from app.services import zapi
    _cfg(app)
    with app.app_context(), \
         patch('app.services.zapi.requests.post', return_value=_Resp()) as post:
        for _ in range(3):
            zapi.enviar_texto('5511999999999', 'alerta')
        r = zapi.enviar_texto('5511999999999', 'a 4a estoura o teto')
    assert r['ok'] is False
    assert r.get('segurado') is True
    assert post.call_count == 3          # a 4a NÃO foi pro WhatsApp
    assert zapi._seg_n == 1              # ficou retida


def test_critico_isento_e_libera_digest(app):
    from app.services import zapi
    _cfg(app)
    with app.app_context(), \
         patch('app.services.zapi.requests.post', return_value=_Resp()) as post:
        for _ in range(3):
            zapi.enviar_texto('5511999999999', 'alerta comum')
        zapi.enviar_texto('5511999999999', 'segurada')      # retida
        assert zapi._seg_n == 1
        r = zapi.enviar_texto('5511999999999', 'LALAMOVE nao saiu', critico=True)
    assert r['ok'] is True
    # 3 comuns + digest das seguradas + a própria crítica = 5 POSTs
    assert post.call_count == 5
    corpos = [c.kwargs['json']['message'] for c in post.call_args_list]
    assert any('SEGURAD' in c for c in corpos)              # digest saiu
    assert zapi._seg_n == 0                                 # buffer zerado


def test_digest_ao_liberar_capacidade_sem_critico(app):
    """Passada a janela, o próximo envio comum libera o digest do que ficou."""
    from app.services import zapi
    _cfg(app)
    with app.app_context(), \
         patch('app.services.zapi.requests.post', return_value=_Resp()) as post:
        for _ in range(3):
            zapi.enviar_texto('5511999999999', 'alerta')
        zapi.enviar_texto('5511999999999', 'segurada')      # retida
        assert zapi._seg_n == 1
        # Simula a janela de 1h já vencida (timestamps antigos são podados).
        zapi._env_ts[:] = [time.monotonic() - 4000] * 3
        r = zapi.enviar_texto('5511999999999', 'novo alerta apos janela')
    assert r['ok'] is True
    corpos = [c.kwargs['json']['message'] for c in post.call_args_list]
    assert any('SEGURAD' in c for c in corpos)
    assert zapi._seg_n == 0


def test_kill_switch_desliga_o_teto(app):
    from app.services import zapi
    _cfg(app, ZAPI_THROTTLE='0', ZAPI_MAX_HORA=2)
    with app.app_context(), \
         patch('app.services.zapi.requests.post', return_value=_Resp()) as post:
        for _ in range(10):
            r = zapi.enviar_texto('5511999999999', 'sem teto')
            assert r['ok'] is True
    assert post.call_count == 10         # nada segurado


def test_off_por_padrao_sob_testing(app):
    """Sem ZAPI_THROTTLE explícito, TESTING mantém o teto DESLIGADO (senão o
    estado global vazaria entre os ~2500 testes que compartilham o app)."""
    from app.services import zapi
    _cfg(app)
    del app.config['ZAPI_THROTTLE']
    app.config['ZAPI_MAX_HORA'] = 1
    with app.app_context(), \
         patch('app.services.zapi.requests.post', return_value=_Resp()) as post:
        for _ in range(5):
            r = zapi.enviar_texto('5511999999999', 'sem teto em teste')
            assert r['ok'] is True
    assert post.call_count == 5

"""Alerta de baixas presas (03/07/2026) — WhatsApp do dono quando:
- pedido parado em 'separado' com entrega vencida (QR de saída não lido →
  indústria NÃO baixou);
- retirada de sobra presa em transporte (loja baixou, indústria não creditada).
Dedup de 6h por conjunto; kill-switch ALERTA_BAIXAS_PRESAS=0.
"""
from datetime import timedelta
from unittest.mock import patch

from app.extensions import db
from app.models import Loja, PedidoLoja, RetiradaSobra
from app.utils import agora, hoje


def _loja(nome='Loja Alerta'):
    loja = Loja(nome=nome, ativa=True)
    db.session.add(loja)
    db.session.commit()
    return loja


def _pedido(loja, status, entrega_delta_dias):
    p = PedidoLoja(loja_id=loja.id, status=status,
                   data_entrega=hoje() + timedelta(days=entrega_delta_dias))
    db.session.add(p)
    db.session.commit()
    return p


def _retirada(loja, status='em_transporte', coletada_ha_horas=13):
    r = RetiradaSobra(loja_id=loja.id, status=status,
                      data_retirada=hoje(), foto_url='https://x/f.jpg',
                      coletada_em=agora() - timedelta(hours=coletada_ha_horas))
    db.session.add(r)
    db.session.commit()
    return r


def test_verificar_detecta_separado_vencido_e_retirada_presa(app):
    from app.services import alertas_operacionais as ao
    with app.app_context():
        loja = _loja()
        preso = _pedido(loja, 'separado', -1)          # entrega ONTEM
        _pedido(loja, 'separado', +1)                  # futuro: ok, não alerta
        _pedido(loja, 'em_transporte', -1)             # já saiu: fora
        presa = _retirada(loja, coletada_ha_horas=13)  # > 12h
        _retirada(loja, coletada_ha_horas=2)           # recente: fora
        _retirada(loja, status='recebida', coletada_ha_horas=30)  # fechada

        d = ao.verificar_baixas_presas()
        assert [p['id'] for p in d['separados']] == [preso.id]
        assert [r['id'] for r in d['retiradas']] == [presa.id]


def test_mensagem_tem_ids_e_instrucao(app):
    from app.services import alertas_operacionais as ao
    with app.app_context():
        loja = _loja()
        p = _pedido(loja, 'separado', -2)
        r = _retirada(loja)
        msg = ao._montar_mensagem(ao.verificar_baixas_presas())
        assert f'#{p.id}' in msg and f'#{r.id}' in msg
        assert 'QR de saída' in msg
        assert 'QR de recebimento' in msg
        assert 'BAIXAS PRESAS' in msg


def test_rodar_envia_e_deduplica(app):
    from app.services import alertas_operacionais as ao
    with app.app_context():
        app.config['ZAPI_NUMERO_DESTINO'] = '5511900000000'
        loja = _loja()
        _pedido(loja, 'separado', -1)

        with patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as m1:
            res = ao.rodar_e_alertar()
        assert res['enviado'] is True and m1.called

        # MESMO conjunto logo em seguida → dedup, não reenvia
        with patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as m2:
            res2 = ao.rodar_e_alertar()
        assert res2.get('dedup') is True and not m2.called

        # Conjunto MUDOU (piorou) → reenvia mesmo dentro das 6h
        _pedido(loja, 'separado', -3)
        with patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as m3:
            res3 = ao.rodar_e_alertar()
        assert res3['enviado'] is True and m3.called


def test_sem_pendencias_nao_envia(app):
    from app.services import alertas_operacionais as ao
    with app.app_context():
        app.config['ZAPI_NUMERO_DESTINO'] = '5511900000000'
        with patch('app.services.zapi.enviar_texto') as m:
            res = ao.rodar_e_alertar()
        assert res['pendencias'] == 0 and not m.called


def test_kill_switch_env(app, monkeypatch):
    from app.services import alertas_operacionais as ao
    monkeypatch.setenv('ALERTA_BAIXAS_PRESAS', '0')
    with app.app_context():
        loja = _loja()
        _pedido(loja, 'separado', -1)
        with patch('app.services.zapi.enviar_texto') as m:
            res = ao.rodar_e_alertar()
        assert res.get('desligado') is True and not m.called

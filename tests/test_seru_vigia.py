"""Vigia do Seru (13/07/2026, incidente das companies): API respondendo mas
pedidos não chegando = doente; alerta na transição, re-alerta 6h, aviso de
normalização. Anthropic/Z-API sempre mockados.
"""
from datetime import datetime
from unittest.mock import patch

from app.services import seru_vigia
from app.utils import hoje


def _dt(h, m=0):
    d = hoje()
    return datetime(d.year, d.month, d.day, h, m)


def _resp(total):
    return {'data': [{'id': 'x'}] if total else [], 'totalPages': total}


def test_vazao_abaixo_do_piso_e_doente(app):
    with app.app_context(), \
         patch('app.services.seru.listar_pedidos',
               return_value=_resp(1)), \
         patch('app.services.seru_vigia.__name__', 'seru_vigia'), \
         patch('app.utils.agora', return_value=_dt(11, 30)):
        out = seru_vigia.rodar_checks()
    assert out['saudavel'] is False
    assert out['pedidos_hoje'] == 1
    assert 'não estão chegando' in out['problemas'][0]


def test_vazao_normal_e_saudavel(app):
    with app.app_context(), \
         patch('app.services.seru.listar_pedidos',
               return_value=_resp(240)), \
         patch('app.utils.agora', return_value=_dt(14, 10)):
        out = seru_vigia.rodar_checks()
    assert out['saudavel'] is True
    assert out['pedidos_hoje'] == 240


def test_fora_de_horario_nao_avalia_vazao(app):
    """Às 6h da manhã, 0 pedidos é normal — só o check de API viva roda."""
    with app.app_context(), \
         patch('app.services.seru.listar_pedidos',
               return_value=_resp(0)), \
         patch('app.utils.agora', return_value=_dt(6, 0)):
        out = seru_vigia.rodar_checks()
    assert out['saudavel'] is True


def test_api_fora_e_doente_em_qualquer_horario(app):
    with app.app_context(), \
         patch('app.services.seru.listar_pedidos',
               side_effect=RuntimeError('Seru auth 500')), \
         patch('app.utils.agora', return_value=_dt(6, 0)):
        out = seru_vigia.rodar_checks()
    assert out['saudavel'] is False
    assert 'API do Seru fora' in out['problemas'][0]


def test_transicao_realerta_e_recuperacao(app):
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999999999'
    with app.app_context():
        with patch('app.services.zapi.enviar_texto') as tx, \
             patch('app.services.seru.listar_pedidos',
                   return_value=_resp(1)), \
             patch('app.utils.agora', return_value=_dt(11, 0)):
            out1 = seru_vigia.vigiar()            # transição -> alerta
            out2 = seru_vigia.vigiar()            # mesmo problema -> suprime
        assert out1['tipo'] == 'alerta' and out1['enviado'] is True
        assert out2['tipo'] == 'alerta_suprimido'
        assert tx.call_count == 1
        assert 'Vigia do SERU' in tx.call_args_list[0].args[1]

        # 7h depois, ainda doente -> re-alerta
        with patch('app.services.zapi.enviar_texto') as tx2, \
             patch('app.services.seru.listar_pedidos',
                   return_value=_resp(2)), \
             patch('app.utils.agora', return_value=_dt(18, 0)):
            out3 = seru_vigia.vigiar()
        assert out3['tipo'] == 'alerta' and tx2.call_count == 1

        # normalizou -> aviso de recuperação e estado limpo
        with patch('app.services.zapi.enviar_texto') as tx3, \
             patch('app.services.seru.listar_pedidos',
                   return_value=_resp(300)), \
             patch('app.utils.agora', return_value=_dt(18, 30)):
            out4 = seru_vigia.vigiar()
            out5 = seru_vigia.vigiar()
        assert out4['tipo'] == 'recuperacao'
        assert 'normalizou' in tx3.call_args_list[0].args[1]
        assert out5['tipo'] == 'saudavel'


def test_rota_owner_vigia_seru(app, admin_user):
    c = app.test_client()
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})
    with patch('app.services.seru.listar_pedidos', return_value=_resp(300)), \
         patch('app.utils.agora', return_value=_dt(14, 0)):
        resp = c.get('/admin/vigia-seru')
    assert resp.status_code == 200
    assert resp.get_json()['saudavel'] is True


def test_lock_7749_unico(app):
    from app.services import seru_cron
    assert seru_cron.LOCK_KEY_SERU_VIGIA == 7749
    locks = [v for k, v in vars(seru_cron).items() if k.startswith('LOCK_KEY')]
    assert len(locks) == len(set(locks))

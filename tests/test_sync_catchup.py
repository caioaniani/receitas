"""Testa que o cron reprocessa D-N (catch-up), nao so hoje."""
import os
from datetime import timedelta
from unittest.mock import patch


def test_seru_catchup_processa_range(app):
    """_run_sync chama processar_pedidos com (hoje - N, hoje)."""
    from app.services import seru_cron
    from app.utils import hoje as hoje_brt

    capturado = {}

    def fake_processar(inicio, fim, user=None, **kw):
        capturado['inicio'] = inicio
        capturado['fim'] = fim
        return {'pedidos_novos': 0, 'itens_baixados': 0,
                'pedidos_cancelados_estornados': 0}

    with app.app_context():
        with patch.dict(os.environ, {'SYNC_CATCHUP_DIAS': '2'}), \
             patch('app.services.seru_sync.processar_pedidos',
                   side_effect=fake_processar):
            seru_cron._run_sync(app)

    hoje = hoje_brt()
    assert capturado['fim'] == hoje
    assert capturado['inicio'] == hoje - timedelta(days=2)


def test_vnda_catchup_chama_cada_dia(app):
    """_run_vnda_sync chama processar_pedidos 1x por dia (hoje..hoje-N)."""
    from app.services import seru_cron
    from app.utils import hoje as hoje_brt

    datas = []

    def fake_processar(data_entrega, user=None):
        datas.append(data_entrega)
        return {'pedidos_novos': 0, 'itens_baixados': 0,
                'pedidos_cancelados_estornados': 0}

    with app.app_context():
        with patch.dict(os.environ, {'SYNC_CATCHUP_DIAS': '2'}), \
             patch('app.services.vnda_sync.processar_pedidos',
                   side_effect=fake_processar):
            seru_cron._run_vnda_sync(app)

    hoje = hoje_brt()
    assert hoje in datas
    assert hoje - timedelta(days=1) in datas
    assert hoje - timedelta(days=2) in datas
    assert len(datas) == 3


def test_catchup_dias_default_e_invalido(app):
    from app.services import seru_cron
    with patch.dict(os.environ, {'SYNC_CATCHUP_DIAS': 'abc'}):
        assert seru_cron._catchup_dias() == 2  # invalido → default
    with patch.dict(os.environ, {'SYNC_CATCHUP_DIAS': '0'}):
        assert seru_cron._catchup_dias() == 0
    with patch.dict(os.environ, {'SYNC_CATCHUP_DIAS': '5'}):
        assert seru_cron._catchup_dias() == 5


def test_com_lock_roda_fn_e_captura_excecao(app):
    """_com_lock roda a fn (em SQLite, sem advisory lock real) e captura
    excecao sem propagar — um job que falha nao derruba o scheduler."""
    from app.services import seru_cron
    with app.app_context():
        chamadas = []
        seru_cron._com_lock(9999, lambda: chamadas.append('ok'), 'teste')
        assert chamadas == ['ok']

        def explode():
            raise ValueError('boom')
        seru_cron._com_lock(9999, explode, 'teste')  # nao deve propagar

"""Horários e lembretes respeitam o corte, sem executar jobs ou enviar Slack."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.services import pedido_corte, seru_cron, slack_resumos


def test_agenda_antecipa_refresh_e_preserva_ordens_e_fermentacao(app, monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler

    monkeypatch.setattr(BackgroundScheduler, 'start', lambda self: None)
    monkeypatch.setattr(seru_cron, '_scheduler', None)
    for nome in ('SERU_AUTO_SYNC', 'AUTO_PEDIDOS', 'AUTO_ENVIO_PLANO'):
        monkeypatch.setenv(nome, '1')
    seru_cron.iniciar(app)
    scheduler = seru_cron._scheduler
    inicio = datetime(2026, 9, 24, tzinfo=ZoneInfo('America/Sao_Paulo'))

    def proximos(job_id, quantidade=1):
        trigger = scheduler.get_job(job_id).trigger
        atual = inicio
        saidas = []
        for _ in range(quantidade):
            atual = trigger.get_next_fire_time(None, atual)
            saidas.append((atual.day, atual.hour, atual.minute))
            atual += timedelta(seconds=1)
        return saidas

    assert proximos('auto-pedidos', 3) == [(24, 6, 30), (24, 11, 30), (25, 6, 30)]
    assert proximos('auto-atualiza-plano-manha') == [(24, 6, 45)]
    assert proximos('auto-atualiza-plano-corte') == [(24, 12, 5)]
    assert proximos('ordens-semana') == [(24, 12, 0)]
    assert proximos('slack-fermentacao') == [(24, 12, 0)]
    lembretes = {j.id for j in scheduler.get_jobs()
                if j.id.startswith('slack-lembrete-') and j.id.endswith('h')}
    assert lembretes == {'slack-lembrete-9h', 'slack-lembrete-11h'}
    assert proximos('slack-lembrete-9h') == [(24, 9, 0)]
    assert proximos('slack-lembrete-11h') == [(24, 11, 0)]
    assert proximos('slack-lembrete-pedidos-hoje', 11) == [
        *((24, hora, 0) for hora in range(10, 20)), (25, 10, 0)]


@pytest.mark.parametrize('hora,minuto,enviados', [(11, 59, 1), (12, 0, 0), (19, 0, 0)])
def test_lembrete_so_convida_antes_do_corte(app, monkeypatch, hora, minuto, enviados):
    from app.services import slack

    hoje = date(2026, 9, 24)
    monkeypatch.setattr(slack_resumos, 'hoje_brt', lambda: hoje)
    monkeypatch.setattr(pedido_corte, 'agora', lambda: datetime(2026, 9, 24, hora, minuto))
    monkeypatch.setitem(app.config, 'SLACK_CANAL_PEDIDOS', 'canal-teste')
    monkeypatch.setattr(slack, 'disponivel', lambda: True)
    monkeypatch.setattr(slack_resumos, 'lojas_sem_pedido_amanha', lambda: [
        (SimpleNamespace(id=1, nome='Loja teste'), hoje + timedelta(days=1))])
    mensagens = []
    monkeypatch.setattr(slack, 'post_message', lambda *a, **kw: mensagens.append(kw))

    slack_resumos.enviar_lembretes_pedido_amanha()

    assert len(mensagens) == enviados
    if mensagens:
        assert 'antes das 12:00 de hoje (Brasília)' in str(mensagens[0]['blocks'])


def test_lembrete_interrompe_lista_ao_chegar_o_corte(app, monkeypatch):
    from app.services import slack

    hoje = date(2026, 9, 24)
    relogio = [datetime(2026, 9, 24, 11, 59, 59)]
    monkeypatch.setattr(slack_resumos, 'hoje_brt', lambda: hoje)
    monkeypatch.setattr(pedido_corte, 'agora', lambda: relogio[0])
    monkeypatch.setitem(app.config, 'SLACK_CANAL_PEDIDOS', 'canal-teste')
    monkeypatch.setattr(slack, 'disponivel', lambda: True)
    monkeypatch.setattr(slack_resumos, 'lojas_sem_pedido_amanha', lambda: [
        (SimpleNamespace(id=n, nome=f'Loja {n}'), hoje + timedelta(days=1))
        for n in (1, 2)])
    mensagens = []

    def enviar(*args, **kwargs):
        mensagens.append(kwargs)
        relogio[0] = datetime(2026, 9, 24, 12)

    monkeypatch.setattr(slack, 'post_message', enviar)
    slack_resumos.enviar_lembretes_pedido_amanha()

    assert len(mensagens) == 1

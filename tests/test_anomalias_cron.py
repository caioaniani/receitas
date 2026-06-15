"""Cron de 'Alertas do dia' (anomalias) — desligado em 14/06/2026.

O dono pediu pra parar de receber esse digest no WhatsApp. A funcao
`anomalias.enviar_digest_whatsapp` continua viva — admin/copilot ainda
podem chamar sob demanda — so o cron 23:00 BRT saiu.
"""
import pathlib


def test_cron_de_digest_anomalias_DESLIGADO():
    """NUNCA reativar o job sem decisao explicita do dono."""
    src = pathlib.Path('app/services/seru_cron.py').read_text()
    assert "id='zapi-digest-anomalias'" not in src
    assert 'def _run_zapi_digest_anomalias' not in src
    # Confirma que a string de log NAO menciona mais o job
    assert 'zapi anomalias 23:00' not in src


def test_funcao_continua_disponivel_pra_admin_e_copilot():
    """O servico fica vivo: o dono pode pedir o resumo via tool do copilot
    (`enviar_digest_whatsapp`) ou na rota admin de notificacoes."""
    from app.services import anomalias
    assert callable(getattr(anomalias, 'enviar_digest_whatsapp', None))

    routes = pathlib.Path('app/blueprints/notificacoes/routes.py').read_text()
    assert 'anomalias.enviar_digest_whatsapp' in routes

    copilot_src = pathlib.Path('app/services/copilot.py').read_text()
    assert "'name': 'enviar_digest_whatsapp'" in copilot_src

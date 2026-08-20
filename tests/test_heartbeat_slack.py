"""Heartbeat invertido via Slack.

Auditoria de 12/06/2026 detectou dependencia circular: se Z-API cair,
nenhum vigia consegue avisar o dono (vigia de infra do Chatwoot, auditor,
abandono e follow-up TODOS usam Z-API). Slack e canal INDEPENDENTE — se
o heartbeat sumir do canal por >12h, o dono sabe que a infra de alertas
caiu, mesmo sem WhatsApp chegando."""
import os
from unittest.mock import patch


def test_sem_canal_configurado_nao_posta(app):
    """Sem SLACK_CHANNEL_HEARTBEAT, job nao posta nada (silencioso por
    design — heartbeat sem destino e ruido)."""
    from app.services import seru_cron
    with patch.dict(os.environ, {'SLACK_CHANNEL_HEARTBEAT': ''}), \
         patch.dict(app.config, {'SLACK_CHANNEL_HEARTBEAT': ''}), \
         patch('app.services.slack.post_message') as fake_post:
        seru_cron._run_heartbeat_slack(app)
    fake_post.assert_not_called()


def test_com_canal_configurado_posta_msg_com_timestamp(app):
    from app.services import seru_cron
    with patch.dict(os.environ, {'SLACK_CHANNEL_HEARTBEAT': 'C123'}), \
         patch('app.services.slack.post_message') as fake_post:
        seru_cron._run_heartbeat_slack(app)
    fake_post.assert_called_once()
    canal_arg = fake_post.call_args[0][0]
    texto = fake_post.call_args[1].get('text') or ''
    assert canal_arg == 'C123'
    # tem 'OK' e horario BRT
    assert 'OK' in texto
    assert 'BRT' in texto
    # auto-explica o significado pro caso de o canal ter membro novo
    assert 'sumir' in texto.lower() or 'fora' in texto.lower()


def test_posta_UMA_vez_por_dia_mesmo_com_varias_execucoes(app):
    """CONTRATO NOVO (20/08/2026, dono: "duplo texto do bot, muito serio").

    O contrato antigo era "posta N vezes e tudo bem — o que importa e a
    presenca". Isso estava ERRADO na pratica: este job era o UNICO sem
    advisory lock nenhum (seru_cron.py:_run_heartbeat_slack), entao os 2
    workers gunicorn postavam o heartbeat — e o aviso de dead-man do
    backup junto — DUAS vezes todo dia no Slack. Agora: claim por DIA."""
    from app.services import seru_cron
    with patch.dict(os.environ, {'SLACK_CHANNEL_HEARTBEAT': 'C123'}), \
         patch('app.services.slack.post_message') as fake_post:
        fake_post.return_value = {'ok': True}
        seru_cron._run_heartbeat_slack(app)
        seru_cron._run_heartbeat_slack(app)   # 2o worker, mesmo dia
        seru_cron._run_heartbeat_slack(app)
    assert fake_post.call_count == 1


def test_slack_fora_devolve_o_claim_e_retenta(app):
    """Heartbeat que nao sai e justamente o sinal de que a infra caiu — nao
    pode se perder por um erro de rede."""
    from app.services import seru_cron
    with patch.dict(os.environ, {'SLACK_CHANNEL_HEARTBEAT': 'C123'}), \
         patch('app.services.slack.post_message') as fake_post:
        fake_post.return_value = {'ok': False, 'erro': 'slack fora'}
        seru_cron._run_heartbeat_slack(app)
        fake_post.return_value = {'ok': True}
        seru_cron._run_heartbeat_slack(app)
    assert fake_post.call_count == 2


def test_canal_via_app_config_tambem_funciona(app):
    """Aceita SLACK_CHANNEL_HEARTBEAT do env OU do Flask config — o
    config tem precedencia menor (env wins, padrao do repo)."""
    from app.services import seru_cron
    app.config['SLACK_CHANNEL_HEARTBEAT'] = 'C-CONFIG'
    with patch.dict(os.environ, {}, clear=False), \
         patch('app.services.slack.post_message') as fake_post:
        os.environ.pop('SLACK_CHANNEL_HEARTBEAT', None)
        seru_cron._run_heartbeat_slack(app)
    fake_post.assert_called_once()
    assert fake_post.call_args[0][0] == 'C-CONFIG'

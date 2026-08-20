"""Guarda de INSTÂNCIA CANÔNICA (20/08/2026) — só a produção fala com o mundo.

Caso real: o serviço de homologação da UI v2 ficou vivo no Railway com as
envs de Chatwoot/Z-API copiadas e rodando os MESMOS crons (cron de horário
de parede => dispara no mesmo minuto que a produção). O dono recebeu o
alerta "Cliente esperando ATENDENTE" DUAS vezes com texto idêntico
(conversas #1759 e #1760). Dedupe de banco não resolve: cada instância tem
o seu — em produção havia UMA linha de VigiaVeredito por conversa.

Discriminador: RAILWAY_GIT_BRANCH (injetada por SERVIÇO pelo Railway, não
copiável junto com as envs do usuário).
"""
from unittest.mock import patch

import pytest

from app.services import instancia

BRANCH_PROD = instancia.BRANCH_PRODUCAO
BRANCH_COPIA = 'codex/ui-simplification-preview'


@pytest.fixture(autouse=True)
def _limpa_log_por_teste():
    """O anti-flood do log é módulo-level; zera pra não vazar entre testes."""
    instancia._ja_logou.clear()
    yield
    instancia._ja_logou.clear()


# ── A regra ──────────────────────────────────────────────────────────────

def test_sem_railway_branch_libera(monkeypatch):
    """Dev local e suíte de testes não têm a env: FAIL-OPEN (perder alerta
    de produção é pior que duplicar)."""
    monkeypatch.delenv('RAILWAY_GIT_BRANCH', raising=False)
    monkeypatch.delenv('ALERTAS_INSTANCIA_CANONICA', raising=False)
    ok, motivo = instancia.status()
    assert ok is True
    assert 'fail-open' in motivo


def test_branch_de_producao_libera(monkeypatch):
    monkeypatch.setenv('RAILWAY_GIT_BRANCH', BRANCH_PROD)
    monkeypatch.delenv('ALERTAS_INSTANCIA_CANONICA', raising=False)
    ok, _ = instancia.status()
    assert ok is True
    assert instancia.pode_falar_com_o_mundo('zapi') is True


def test_branch_desconhecido_LIBERA(monkeypatch):
    """Achado de revisão 20/08: branch hardcoded era ponto único de falha —
    renomear o branch de produção calaria TUDO (inclusive o bot respondendo
    cliente e o próprio heartbeat que detectaria o problema). Regra: só
    bloqueia branch que CASA padrão de cópia; desconhecido = produção."""
    monkeypatch.setenv('RAILWAY_GIT_BRANCH', 'claude/branch-renomeado-2027')
    monkeypatch.delenv('ALERTAS_INSTANCIA_CANONICA', raising=False)
    ok, motivo = instancia.status()
    assert ok is True
    assert 'desconhecido' in motivo
    assert instancia.pode_falar_com_o_mundo('chatwoot') is True


def test_override_zero_silencia_ate_o_critico(monkeypatch):
    """`=0` é gesto humano explícito ("esta cópia não fala com ninguém") —
    diferente do bloqueio automático, que deixa o crítico passar."""
    monkeypatch.setenv('ALERTAS_INSTANCIA_CANONICA', '0')
    assert instancia.pode_falar_com_o_mundo('zapi', critico=True) is False


def test_branch_de_copia_bloqueia(monkeypatch):
    monkeypatch.setenv('RAILWAY_GIT_BRANCH', BRANCH_COPIA)
    monkeypatch.delenv('ALERTAS_INSTANCIA_CANONICA', raising=False)
    ok, motivo = instancia.status()
    assert ok is False
    assert BRANCH_COPIA in motivo
    assert instancia.pode_falar_com_o_mundo('zapi') is False


def test_critico_passa_mesmo_em_copia(monkeypatch):
    """Se um dia a detecção estiver errada, o que não pode faltar
    (Lalamove, pedido pago) continua saindo."""
    monkeypatch.setenv('RAILWAY_GIT_BRANCH', BRANCH_COPIA)
    assert instancia.pode_falar_com_o_mundo('zapi', critico=True) is True


def test_override_liga_e_desliga(monkeypatch):
    monkeypatch.setenv('RAILWAY_GIT_BRANCH', BRANCH_COPIA)
    monkeypatch.setenv('ALERTAS_INSTANCIA_CANONICA', '1')
    assert instancia.status()[0] is True          # escape se renomearem o branch
    monkeypatch.setenv('RAILWAY_GIT_BRANCH', BRANCH_PROD)
    monkeypatch.setenv('ALERTAS_INSTANCIA_CANONICA', '0')
    assert instancia.status()[0] is False         # silencia uma cópia sem deploy


def test_guarda_quebrada_nunca_cala_a_producao(monkeypatch):
    monkeypatch.setattr(instancia, 'status',
                        lambda: (_ for _ in ()).throw(RuntimeError('boom')))
    assert instancia.pode_falar_com_o_mundo('zapi') is True


# ── Os três canais que leem estado EXTERNO compartilhado ─────────────────

def test_zapi_nao_manda_da_copia(app, monkeypatch):
    from app.services import zapi
    monkeypatch.setenv('RAILWAY_GIT_BRANCH', BRANCH_COPIA)
    monkeypatch.delenv('ALERTAS_INSTANCIA_CANONICA', raising=False)
    app.config['ZAPI_INSTANCE_ID'] = 'inst'
    app.config['ZAPI_TOKEN'] = 'tok'
    with patch('requests.post') as post:
        res = zapi.enviar_texto('5511999999999', 'oi')
    assert res['ok'] is False and res.get('suprimido_instancia') is True
    assert not post.called          # nem chega na rede


def test_zapi_manda_da_producao(app, monkeypatch):
    from app.services import zapi
    monkeypatch.setenv('RAILWAY_GIT_BRANCH', BRANCH_PROD)
    monkeypatch.delenv('ALERTAS_INSTANCIA_CANONICA', raising=False)
    app.config['ZAPI_INSTANCE_ID'] = 'inst'
    app.config['ZAPI_TOKEN'] = 'tok'
    app.config['ZAPI_NUMEROS_PERMITIDOS'] = '5511999999999'
    with patch('requests.post') as post:
        post.return_value.status_code = 200
        post.return_value.text = '{}'
        post.return_value.json.return_value = {}
        res = zapi.enviar_texto('5511999999999', 'oi')
    assert res['ok'] is True and post.called


def test_slack_nao_posta_da_copia(app, monkeypatch):
    from app.services import slack
    monkeypatch.setenv('RAILWAY_GIT_BRANCH', BRANCH_COPIA)
    monkeypatch.delenv('ALERTAS_INSTANCIA_CANONICA', raising=False)
    with patch.object(slack, '_client') as cli:
        res = slack.post_message('C0X', 'oi')
    assert res['ok'] is False and res.get('suprimido_instancia') is True
    assert not cli.called


def test_chatwoot_nao_responde_cliente_da_copia(app, monkeypatch):
    """O mais importante: o Chatwoot é externo e compartilhado — a cópia
    enxerga conversas REAIS e responderia ao cliente em dobro."""
    from app.services import chatwoot
    monkeypatch.setenv('RAILWAY_GIT_BRANCH', BRANCH_COPIA)
    monkeypatch.delenv('ALERTAS_INSTANCIA_CANONICA', raising=False)
    with patch('requests.post') as post:
        res = chatwoot.enviar_mensagem(123, 'oi')
    assert res['ok'] is False and res.get('suprimido_instancia') is True
    assert not post.called


# ── Cooldown do cron: job de INTERVALO não roda duas vezes na janela ─────
# Job de 'interval' conta do boot de CADA worker/container, então os
# disparos caem em minutos diferentes e o advisory lock (que só serializa o
# SIMULTÂNEO) já foi liberado — o segundo processo repete o ciclo inteiro
# do vigia e o dono recebe o alerta duas vezes.

def test_com_lock_cooldown_bloqueia_segunda_execucao(app):
    from app.services import seru_cron
    chamadas = []
    with app.app_context():
        seru_cron._com_lock(9911, lambda: chamadas.append(1),
                            'teste cooldown', cooldown_seg=600)
        seru_cron._com_lock(9911, lambda: chamadas.append(1),
                            'teste cooldown', cooldown_seg=600)
    assert chamadas == [1]


def test_com_lock_sem_cooldown_mantem_comportamento_antigo(app):
    """Sem o parâmetro, nada muda (jobs de cron seguem como sempre)."""
    from app.services import seru_cron
    chamadas = []
    with app.app_context():
        seru_cron._com_lock(9912, lambda: chamadas.append(1), 'teste sem cd')
        seru_cron._com_lock(9912, lambda: chamadas.append(1), 'teste sem cd')
    assert chamadas == [1, 1]


def test_cooldown_com_banco_quebrado_nao_cala_o_job(app, monkeypatch):
    """Fail-open: guarda quebrada não pode silenciar vigia de produção."""
    from app.services import seru_cron, whatsapp
    monkeypatch.setattr(whatsapp, 'claim_por_cooldown',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))
    chamadas = []
    with app.app_context():
        seru_cron._com_lock(9913, lambda: chamadas.append(1),
                            'teste cd quebrado', cooldown_seg=600)
    assert chamadas == [1]


def test_chatwoot_nao_muda_status_de_conversa_real_da_copia(app, monkeypatch):
    """Achado de revisão: a vassoura chama `definir_status` FORA do `if
    texto` — com só o envio guardado, a cópia marcava conversa real como
    'resolved' (some da fila da equipe) sem mandar nada ao cliente."""
    from app.services import chatwoot
    monkeypatch.setenv('RAILWAY_GIT_BRANCH', BRANCH_COPIA)
    monkeypatch.delenv('ALERTAS_INSTANCIA_CANONICA', raising=False)
    with patch('requests.post') as post:
        res = chatwoot.definir_status(123, 'resolved')
    assert res['ok'] is False and res.get('suprimido_instancia') is True
    assert not post.called


def test_chatwoot_template_pago_nao_sai_da_copia(app, monkeypatch):
    """Template da Meta custa dinheiro por disparo."""
    from app.services import chatwoot
    monkeypatch.setenv('RAILWAY_GIT_BRANCH', BRANCH_COPIA)
    monkeypatch.delenv('ALERTAS_INSTANCIA_CANONICA', raising=False)
    with patch('requests.post') as post:
        res = chatwoot.enviar_template(1, 'tpl', ['a'], 'pt_BR')
    assert res.get('suprimido_instancia') is True and not post.called

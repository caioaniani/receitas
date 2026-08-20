"""Aba de notificacoes WhatsApp: pagina, CRUD de automacoes e motor de disparo."""
from unittest.mock import patch


def _login(c):
    c.post('/auth/login', data={'login': 'admin', 'senha': '123'})


def test_pagina_renderiza(app, admin_user):
    c = app.test_client()
    _login(c)
    r = c.get('/notificacoes/')
    assert r.status_code == 200
    assert 'Notificações WhatsApp'.encode() in r.data


def test_criar_e_toggle_automacao(app, admin_user):
    from app.extensions import db
    from app.models import AutomacaoWhatsapp
    c = app.test_client()
    _login(c)
    c.post('/notificacoes/automacoes', data={
        'nome': 'Abertura', 'horario': '08:00', 'mensagem': 'Bom dia!',
        'dia_0': '1', 'dia_1': '1'}, follow_redirects=True)
    with app.app_context():
        a = AutomacaoWhatsapp.query.filter_by(nome='Abertura').first()
        assert a is not None and a.horario == '08:00'
        assert a.dias_semana == '0,1' and a.ativo is True
        aid = a.id

    c.post(f'/notificacoes/automacoes/{aid}', data={'acao': 'toggle'},
           follow_redirects=True)
    with app.app_context():
        assert db.session.get(AutomacaoWhatsapp, aid).ativo is False


def test_motor_dispara_e_idempotente(app):
    from app.extensions import db
    from app.models import AutomacaoWhatsapp, NotificacaoWhatsapp
    from app.services import whatsapp
    app.config['ZAPI_NUMERO_DESTINO'] = '5511999999999'
    with app.app_context():
        db.session.add(AutomacaoWhatsapp(
            nome='T', horario='00:00', mensagem='oi', ativo=True))
        db.session.commit()
        with patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as mock_env:
            assert whatsapp.disparar_automacoes_devidas() == 1
            assert mock_env.call_count == 1
            # ja disparou hoje -> nao repete
            assert whatsapp.disparar_automacoes_devidas() == 0
            assert mock_env.call_count == 1
        assert NotificacaoWhatsapp.query.count() == 1  # registrou no log


def test_notificar_registra_log(app):
    from app.models import NotificacaoWhatsapp
    from app.services import whatsapp
    with app.app_context():
        with patch('app.services.zapi.enviar_texto', return_value={'ok': True}):
            whatsapp.notificar('5511999999999', 'teste', origem='manual')
        n = NotificacaoWhatsapp.query.first()
        assert n is not None and n.ok is True and n.origem == 'manual'


# ── Digest de tarefas 07:00: anti-duplicata por claim (20/08/2026) ───────
# Caso real: dois "Bom dia!" as 07:00 no WhatsApp do dono — dois schedulers
# vivos no mesmo minuto (overlap de deploy / 2 workers gunicorn). O claim
# diario em AppConfig (whatsapp.claim_envio) segura o segundo.

def _arma_digest(app, monkeypatch, enviados):
    from app.services import zapi
    app.config['ZAPI_NUMERO_DESTINO'] = '5511999999999'
    monkeypatch.setattr(zapi, 'disponivel', lambda: True)
    monkeypatch.setattr(zapi, 'enviar_texto',
                        lambda n, m: enviados.append(m) or {'ok': True})


def test_digest_tarefas_nao_duplica_no_mesmo_dia(app, owner_user, monkeypatch):
    from app.services import zapi_resumos
    enviados = []
    _arma_digest(app, monkeypatch, enviados)
    zapi_resumos.enviar_digest_tarefas()
    zapi_resumos.enviar_digest_tarefas()
    assert len(enviados) == 1


def test_digest_tarefas_claim_false_reenvia(app, owner_user, monkeypatch):
    """O botao manual do /notificacoes (claim=False) re-envia mesmo depois
    do cron do dia."""
    from app.services import zapi_resumos
    enviados = []
    _arma_digest(app, monkeypatch, enviados)
    zapi_resumos.enviar_digest_tarefas()
    zapi_resumos.enviar_digest_tarefas(claim=False)
    assert len(enviados) == 2


def test_digest_tarefas_envio_falho_devolve_o_claim(app, owner_user, monkeypatch):
    from app.services import zapi, zapi_resumos
    app.config['ZAPI_NUMERO_DESTINO'] = '5511999999999'
    monkeypatch.setattr(zapi, 'disponivel', lambda: True)
    monkeypatch.setattr(zapi, 'enviar_texto', lambda n, m: {'ok': False})
    zapi_resumos.enviar_digest_tarefas()
    enviados = []
    monkeypatch.setattr(zapi, 'enviar_texto',
                        lambda n, m: enviados.append(m) or {'ok': True})
    zapi_resumos.enviar_digest_tarefas()
    assert len(enviados) == 1

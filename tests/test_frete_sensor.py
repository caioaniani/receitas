"""Sensor de geocode do frete (09/07/2026) — log de eventos que barram/erram
venda + painel do dono."""


def test_sensor_registra_e_resumo_conta(app):
    from app.models import FreteSensor
    from app.services import frete_sensor
    with app.app_context():
        frete_sensor.registrar('preview', 'barrado', endereco='Rua X',
                               cep='01000-000')
        frete_sensor.registrar('checkout', 'impreciso', endereco='Rua Y',
                               cep='02000-000', valor=20.0, km=4.6,
                               fonte='cep_centroide', contato='Fulano · 11')
        frete_sensor.registrar('lalamove', 'lalamove_falhou', endereco='Rua Z')
        assert FreteSensor.query.count() == 3
        r = frete_sensor.resumo(7)
        assert r['total'] == 3
        assert r['barrado'] == 1 and r['impreciso'] == 1
        assert r['lalamove_falhou'] == 1
        assert len(r['eventos']) == 3


def test_sensor_ignora_desfecho_desconhecido_e_kill_switch(app):
    from app.models import FreteSensor
    from app.services import frete_sensor
    with app.app_context():
        # desfecho fora da whitelist não grava (evita ruído de sucesso normal)
        frete_sensor.registrar('preview', 'sucesso_normal', endereco='Z')
        assert FreteSensor.query.count() == 0
        # kill-switch
        app.config['FRETE_SENSOR'] = '0'
        frete_sensor.registrar('preview', 'barrado', endereco='Z')
        assert FreteSensor.query.count() == 0


def test_sensor_registrar_best_effort_nao_quebra(app):
    from app.services import frete_sensor
    with app.app_context():
        # argumentos None não podem levantar
        frete_sensor.registrar('preview', 'barrado')


def test_rota_frete_sensores_owner(app, owner_user):
    from app.services import frete_sensor
    with app.app_context():
        frete_sensor.registrar('checkout', 'barrado',
                               endereco='Rua Guararapes, 225', cep='04561-000',
                               contato='Alane · 119')
    c = app.test_client()
    c.post('/auth/login', data={'login': owner_user.login, 'senha': '123'})
    resp = c.get('/admin/frete-sensores')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Sensores do Frete' in body and 'Rua Guararapes' in body


def test_rota_frete_sensores_anonimo_barra(app):
    # Sem login → owner_required barra (403 ou redirect pro login, nunca 200).
    assert app.test_client().get('/admin/frete-sensores').status_code != 200


def test_frete_sensor_entra_na_retencao(app):
    """PII do cliente (endereço/contato) não fica pra sempre — poda por LGPD."""
    from datetime import timedelta

    from app.extensions import db
    from app.models import FreteSensor
    from app.services import retencao
    from app.utils import agora
    with app.app_context():
        app.config['RETENCAO_FRETE_SENSOR_DIAS'] = 30
        db.session.add_all([
            FreteSensor(origem='preview', desfecho='barrado', endereco='Velho',
                        criado_em=agora() - timedelta(days=60)),
            FreteSensor(origem='preview', desfecho='barrado', endereco='Novo',
                        criado_em=agora() - timedelta(days=5)),
        ])
        db.session.commit()
        rel = retencao.executar_limpeza()
        assert rel.get('frete_sensor') == 1              # o velho foi apagado
        assert FreteSensor.query.count() == 1            # só o novo sobrou

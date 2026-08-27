"""Envio Z-API pra GRUPOS de WhatsApp (pedido do dono, 12/06/2026).

O dono quer os alertas dos vigias num grupo (equipe ve junto) em vez do
numero pessoal. ID de grupo do Z-API tem sufixo '-group' — a normalizacao
de telefone destruiria o sufixo, e a whitelist de numeros nao o conhece.
Grupos ganharam caminho proprio: _normalizar_grupo + _whitelist_grupos
(ZAPI_GRUPOS_PERMITIDOS + destinos de alerta configurados)."""
from unittest.mock import patch

import pytest


def _cfg_zapi(app, **extra):
    app.config['ZAPI_INSTANCE_ID'] = 'inst1'
    app.config['ZAPI_TOKEN'] = 'tok1'
    app.config['ZAPI_CLIENT_TOKEN'] = ''
    for k, v in extra.items():
        app.config[k] = v


class _Resp:
    status_code = 200
    text = '{"zaapId": "zaap-1", "messageId": "msg-1"}'

    @staticmethod
    def json():
        return {'zaapId': 'zaap-1', 'messageId': 'msg-1'}


@pytest.fixture(autouse=True)
def _zapi_conectada(monkeypatch):
    from app.services import zapi
    monkeypatch.setattr(zapi, 'status_instancia', lambda: {
        'ok': True, 'conectado': True, 'detalhe': 'conectado'})


def test_grupo_no_destino_de_alerta_passa_automatico(app):
    """Grupo configurado como CHATBOT_VIGIA_NUMERO entra sozinho na
    whitelist (mesmo atalho dos numeros) — configurar 1 env basta."""
    from app.services import zapi
    _cfg_zapi(app, CHATBOT_VIGIA_NUMERO='120363012345678901-group')
    with app.app_context(), \
         patch('app.services.zapi.requests.post',
               return_value=_Resp()) as post:
        r = zapi.enviar_texto('120363012345678901-group', 'alerta teste')
    assert r['ok'] is True
    corpo = post.call_args[1]['json']
    # ID vai INTEIRO, com sufixo (a normalizacao de fone teria destruido)
    assert corpo['phone'] == '120363012345678901-group'


def test_grupo_fora_da_whitelist_recusado(app):
    """Fail-closed: grupo desconhecido nao recebe nada — mesmo rigor da
    whitelist de numeros."""
    from app.services import zapi
    _cfg_zapi(app)
    with app.app_context(), \
         patch('app.services.zapi.requests.post') as post:
        r = zapi.enviar_texto('999999999999-group', 'oi')
    assert r['ok'] is False
    assert 'whitelist' in r['erro']
    post.assert_not_called()


def test_grupos_permitidos_via_env(app):
    from app.services import zapi
    _cfg_zapi(app, ZAPI_GRUPOS_PERMITIDOS='111-group, 222-group')
    with app.app_context(), \
         patch('app.services.zapi.requests.post', return_value=_Resp()):
        assert zapi.enviar_texto('111-group', 'a')['ok'] is True
        assert zapi.enviar_texto('222-group', 'b')['ok'] is True
        assert zapi.enviar_texto('333-group', 'c')['ok'] is False


def test_telefone_continua_com_normalizacao_e_whitelist(app):
    """Regressao: o caminho de telefone nao mudou — normaliza e valida
    na whitelist de numeros como sempre."""
    from app.services import zapi
    _cfg_zapi(app, ZAPI_NUMEROS_PERMITIDOS='+55 11 99999-0000')
    with app.app_context(), \
         patch('app.services.zapi.requests.post',
               return_value=_Resp()) as post:
        # entrada formatada diferente do env — normalizacao colapsa as duas
        r = zapi.enviar_texto('5511999990000', 'oi')
    assert r['ok'] is True
    assert post.call_args[1]['json']['phone'] == '5511999990000'


def test_vigia_infra_envia_pro_grupo_quando_configurado(app):
    """Decisao do dono (12/06/2026): SO os vigias vao pro grupo; digests
    continuam no privado. O vigia de infra usa CHATWOOT_VIGIA_INFRA_NUMERO
    (aceita grupo) com fallback pro numero pessoal."""
    from app.services import chatwoot
    _cfg_zapi(app,
              CHATWOOT_URL='https://chat.exemplo.com.br',
              ZAPI_BOT_DONO_NUMERO='5511999990000',
              CHATWOOT_VIGIA_INFRA_NUMERO='120363555-group')
    doente = {'saudavel': False, 'conclusao': 'problema X'}
    # Zera estado persistido do vigia
    from app.extensions import db
    from app.models import AppConfig
    with app.app_context():
        for k in ('vigia_chatwoot_quebrado_desde',
                  'vigia_chatwoot_ultimo_alerta_em',
                  'vigia_chatwoot_ultima_assinatura'):
            AppConfig.set(k, None)
        db.session.commit()
        with patch('app.services.chatwoot.diagnostico',
                   return_value=doente), \
             patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as envia:
            r = chatwoot.vigiar_infra()
    assert r['tipo'] == 'alerta'
    # Foi pro GRUPO, nao pro numero pessoal
    assert envia.call_args[0][0] == '120363555-group'


def test_vigia_infra_fallback_pro_dono_sem_grupo(app):
    """Sem CHATWOOT_VIGIA_INFRA_NUMERO, continua indo pro numero do
    dono — comportamento de antes preservado."""
    from app.extensions import db
    from app.models import AppConfig
    from app.services import chatwoot
    _cfg_zapi(app,
              CHATWOOT_URL='https://chat.exemplo.com.br',
              ZAPI_BOT_DONO_NUMERO='5511999990000',
              CHATWOOT_VIGIA_INFRA_NUMERO='')
    doente = {'saudavel': False, 'conclusao': 'problema Y'}
    with app.app_context():
        for k in ('vigia_chatwoot_quebrado_desde',
                  'vigia_chatwoot_ultimo_alerta_em',
                  'vigia_chatwoot_ultima_assinatura'):
            AppConfig.set(k, None)
        db.session.commit()
        with patch('app.services.chatwoot.diagnostico',
                   return_value=doente), \
             patch('app.services.zapi.enviar_texto',
                   return_value={'ok': True}) as envia:
            chatwoot.vigiar_infra()
    assert envia.call_args[0][0] == '5511999990000'


def test_normalizar_grupo_limpa_lixo_e_preserva_sufixo(app):
    from app.services.zapi import _normalizar_grupo
    assert _normalizar_grupo('1203 6302-group') == '12036302-group'
    assert _normalizar_grupo(' 111-GROUP ') == '111-group'
    assert _normalizar_grupo('sem-digitos-group') == ''
    assert _normalizar_grupo('') == ''


def test_rota_zapi_grupos_owner_only(app):
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        comum = Usuario(nome='adm', login='adm_zg', papel='admin',
                        is_owner=False)
        comum.set_senha('senha123')
        db.session.add(comum)
        db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'adm_zg', 'senha': 'senha123'})
    assert c.get('/admin/zapi/grupos').status_code == 403


def test_rota_zapi_grupos_lista_ids(app):
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        dono = Usuario(nome='dono', login='dono_zg', papel='admin',
                       is_owner=True)
        dono.set_senha('senha123')
        db.session.add(dono)
        db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'dono_zg', 'senha': 'senha123'})
    fake = {'ok': True, 'total': 1,
            'grupos': [{'id': '120363999-group', 'nome': 'Alertas O Pão'}]}
    with patch('app.services.zapi.listar_grupos', return_value=fake):
        r = c.get('/admin/zapi/grupos')
    assert r.status_code == 200
    data = r.get_json()
    assert data['grupos'][0]['id'].endswith('-group')
    assert data['grupos'][0]['nome'] == 'Alertas O Pão'


def test_rota_zapi_grupos_testar_envia_mensagem(app):
    """?testar=<grupo> manda mensagem de teste na hora — confirma a
    configuracao sem esperar incidente real. Grupo configurado em env
    de destino passa pela whitelist automatica."""
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        dono = Usuario(nome='dono', login='dono_zg2', papel='admin',
                       is_owner=True)
        dono.set_senha('senha123')
        db.session.add(dono)
        db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'dono_zg2', 'senha': 'senha123'})
    fake_lista = {'ok': True, 'total': 0, 'grupos': []}
    with patch('app.services.zapi.listar_grupos',
               return_value=fake_lista), \
         patch('app.services.zapi.enviar_texto',
               return_value={'ok': True}) as envia:
        r = c.get('/admin/zapi/grupos?testar=120363999-group')
    assert r.status_code == 200
    data = r.get_json()
    assert data['teste_envio'] == {'ok': True}
    envia.assert_called_once()
    assert envia.call_args[0][0] == '120363999-group'
    assert 'Teste de alerta' in envia.call_args[0][1]

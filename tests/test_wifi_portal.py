"""Portal Wi-Fi das lojas (11/07/2026, Ribeiro do Vale).

Cobre o coração do fluxo: validação do form, sessão + código, as 4 REGRAS
de conta (posse provada do telefone; e-mail sem prova nunca loga em conta
alheia), guest upgrade/proteção, login one-time e o interceptor do webhook
do Chatwoot (código WIFI-XXXXXX responde determinístico, sem Claude, e
funciona até em conversa 'open').
"""
from unittest.mock import patch

import pytest

from app.extensions import db

# O gate da loja exige LOJA_VISIVEL=1 E host público (LOJA_HOSTS). O marker
# `loja_host` (conftest) faz o localhost dos testes contar como host da loja.
pytestmark = pytest.mark.loja_host


@pytest.fixture
def visivel(monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')


def _form(**kw):
    base = {'nome': 'Maria Teste', 'email': 'maria@example.com',
            'telefone': '(11) 98888-7777', 'senha': 'segredo1',
            'aniversario_dia': '15', 'aniversario_mes': '3',
            'nascimento_ano': '1990', 'aceite_lgpd': '1'}
    base.update(kw)
    return base


def _sessao(**kw):
    from app.services import wifi_portal
    dados, erros = wifi_portal.validar_form(_form(**kw))
    assert not erros, erros
    return wifi_portal.criar_sessao(dados, {'clientMac': 'AA:BB:CC:11:22:33',
                                            'ssidName': 'O_Pao_Clientes'})


def _cliente(email='maria@example.com', telefone=None, senha=None,
             nome='Maria Antiga'):
    from app.models import Cliente
    c = Cliente(nome=nome, email=email, telefone=telefone)
    if senha:
        c.set_senha(senha)
    db.session.add(c)
    db.session.commit()
    return c


# ── Form e sessão ────────────────────────────────────────────────────────

def test_validar_form_erros(app):
    from app.services import wifi_portal
    with app.app_context():
        _, erros = wifi_portal.validar_form(_form(
            email='naoeemail', telefone='9999', senha='123',
            aniversario_dia='', aceite_lgpd=''))
        assert len(erros) == 5


def test_criar_sessao_gera_codigo_e_hasheia_senha(app):
    with app.app_context():
        s = _sessao()
        assert s.codigo.startswith('WIFI-') and len(s.codigo) == 11
        assert s.senha_hash and 'segredo1' not in s.senha_hash
        assert s.client_mac == 'AA:BB:CC:11:22:33'
        assert s.aceite_lgpd_em is not None


def test_extrair_codigo_variantes(app):
    from app.services.wifi_portal import extrair_codigo
    assert extrair_codigo('Ativar Wi-Fi O Pão — código WIFI-AB2CD3') == \
        'WIFI-AB2CD3'
    assert extrair_codigo('wifi ab2cd3') == 'WIFI-AB2CD3'
    assert extrair_codigo('oi, tudo bem?') is None


# ── Regras de conta ──────────────────────────────────────────────────────

def test_regra_a_cria_conta_e_loga(app):
    from app.models import Cliente
    from app.services import wifi_portal
    with app.app_context():
        s = _sessao()
        res = wifi_portal.processar_codigo_whatsapp(
            f'Ativar Wi-Fi — {s.codigo}', '+55 11 98888-7777')
        assert res['sessao'] is not None
        assert res['sessao'].resultado == 'conta_criada'
        assert '/loja/wifi/entrar/' in res['texto']
        c = Cliente.query.filter_by(email='maria@example.com').one()
        assert c.tem_conta and c.check_senha('segredo1')
        assert c.aniversario_dia == 15 and c.aniversario_mes == 3
        assert c.nascimento_ano == 1990
        assert c.aceite_lgpd_em is not None


def test_regra_b_login_direto_sem_mexer_na_senha(app):
    from app.services import wifi_portal
    with app.app_context():
        antigo = _cliente(telefone='11988887777', senha='senha-antiga')
        s = _sessao()          # mesmo e-mail, senha nova no form
        res = wifi_portal.processar_codigo_whatsapp(
            s.codigo, '5511988887777')
        assert res['sessao'].resultado == 'login_direto'
        assert res['sessao'].cliente_id == antigo.id
        # senha antiga INTACTA (a do form é ignorada — dono aprovou)
        assert antigo.check_senha('senha-antiga')
        assert not antigo.check_senha('segredo1')


def test_regra_c_telefone_de_outra_conta(app):
    from app.services import wifi_portal
    with app.app_context():
        dono_tel = _cliente(email='joao@example.com',
                            telefone='11988887777', senha='x1y2z3')
        s = _sessao()          # e-mail novo, telefone que pertence ao João
        res = wifi_portal.processar_codigo_whatsapp(s.codigo, '11988887777')
        assert res['sessao'].resultado == 'login_conta_telefone'
        assert res['sessao'].cliente_id == dono_tel.id
        assert 'j•••@example.com' in res['texto']


def test_regra_d_email_existe_telefone_diverge_manda_email(app):
    from app.services import wifi_portal
    with app.app_context():
        antigo = _cliente(telefone='11911112222', senha='senha-antiga')
        s = _sessao()          # mesmo e-mail, telefone provado DIFERENTE
        with patch('app.services.email.enviar',
                   return_value={'ok': True}) as env:
            res = wifi_portal.processar_codigo_whatsapp(
                s.codigo, '11988887777')
        assert res['sessao'].resultado == 'magic_link_email'
        assert env.called                      # magic link foi pro e-mail
        destinatario = env.call_args[0][0]
        assert destinatario == antigo.email
        # a resposta do WhatsApp NÃO carrega o link de login
        assert '/loja/wifi/entrar/' not in res['texto']


def test_guest_upgrade_com_telefone_batendo(app):
    from app.services import wifi_portal
    with app.app_context():
        guest = _cliente(telefone='11988887777', senha=None)   # sem senha
        s = _sessao()
        res = wifi_portal.processar_codigo_whatsapp(s.codigo, '11988887777')
        assert res['sessao'].resultado == 'conta_criada'       # upgrade
        assert guest.tem_conta and guest.check_senha('segredo1')


def test_guest_com_historico_divergente_vai_pra_email(app):
    from app.services import wifi_portal
    with app.app_context():
        _cliente(telefone='11933334444', senha=None)   # guest c/ outro fone
        s = _sessao()
        with patch('app.services.email.enviar', return_value={'ok': True}):
            res = wifi_portal.processar_codigo_whatsapp(
                s.codigo, '11988887777')
        assert res['sessao'].resultado == 'magic_link_email'


def test_codigo_inexistente_resposta_gentil(app):
    from app.services import wifi_portal
    with app.app_context():
        res = wifi_portal.processar_codigo_whatsapp(
            'WIFI-ZZZZZZ', '11988887777')
        assert res['sessao'] is None
        assert 'refaça o cadastro' in res['texto'].lower() or \
            'Volte' in res['texto']


# ── Login one-time ───────────────────────────────────────────────────────

def test_login_token_one_time(app):
    from app.services import wifi_portal
    with app.app_context():
        s = _sessao()
        wifi_portal.processar_codigo_whatsapp(s.codigo, '11988887777')
        token = s.login_token
        cliente, _ = wifi_portal.usar_login_token(token)
        assert cliente is not None
        # segunda visita: já usado
        de_novo, _ = wifi_portal.usar_login_token(token)
        assert de_novo is None


# ── Rotas ────────────────────────────────────────────────────────────────

def test_rota_portal_e_fluxo_web(app, visivel):
    with app.app_context():
        app.config['WIFI_PORTAL_WHATSAPP'] = '5511900001111'
    c = app.test_client()
    r = c.get('/loja/wifi')
    assert r.status_code == 200
    assert 'Wi-Fi' in r.get_data(as_text=True)
    r2 = c.post('/loja/wifi/cadastrar', data=_form(), follow_redirects=True)
    body = r2.get_data(as_text=True)
    assert r2.status_code == 200
    assert 'WIFI-' in body                 # código na tela
    assert 'wa.me/5511900001111' in body   # botão do WhatsApp


def test_rota_entrar_loga_cliente(app, visivel):
    from app.services import wifi_portal
    with app.app_context():
        s = _sessao()
        wifi_portal.processar_codigo_whatsapp(s.codigo, '11988887777')
        token = s.login_token
    c = app.test_client()
    r = c.get(f'/loja/wifi/entrar/{token}')
    assert r.status_code == 302
    with c.session_transaction() as sess:
        assert sess.get('cliente_id') is not None
    # link usado de novo → página de expirado (410)
    assert c.get(f'/loja/wifi/entrar/{token}').status_code == 410


# ── Interceptor do webhook ───────────────────────────────────────────────

def _payload_webhook(content, status='pending'):
    return {
        'event': 'message_created', 'message_type': 'incoming',
        'id': 987650 + hash(content) % 1000,
        'conversation': {'id': 4242, 'status': status,
                         'meta': {'sender': {
                             'phone_number': '+5511988887777'}}},
        'content': content,
        'sender': {'phone_number': '+5511988887777'},
    }


def test_webhook_intercepta_codigo_e_resolve(app):
    with app.app_context():
        app.config['CHATWOOT_BOT_SECRET'] = 'seg'
        s = _sessao()
        codigo = s.codigo
    c = app.test_client()
    with patch('app.services.chatwoot.enviar_mensagem',
               return_value={'ok': True}) as env, \
            patch('app.services.chatwoot.definir_status') as st:
        r = c.post('/crm/bot?k=seg',
                   json=_payload_webhook(f'Ativar Wi-Fi — {codigo}'))
    assert r.status_code == 200
    assert r.get_json().get('wifi_portal') is True
    assert env.called
    assert 'Wi-Fi liberado' in env.call_args[0][1]
    st.assert_called_once_with(4242, 'resolved')


def test_webhook_codigo_em_conversa_open_nao_resolve(app):
    """Conversa com atendente ('open'): responde o código mas NÃO mexe no
    status (não fecha a conversa do humano)."""
    with app.app_context():
        app.config['CHATWOOT_BOT_SECRET'] = 'seg'
        s = _sessao()
        codigo = s.codigo
    c = app.test_client()
    with patch('app.services.chatwoot.enviar_mensagem',
               return_value={'ok': True}) as env, \
            patch('app.services.chatwoot.definir_status') as st:
        r = c.post('/crm/bot?k=seg',
                   json=_payload_webhook(codigo, status='open'))
    assert r.status_code == 200
    assert r.get_json().get('wifi_portal') is True
    assert env.called
    assert not st.called


def test_webhook_sem_codigo_segue_fluxo_normal(app):
    with app.app_context():
        app.config['CHATWOOT_BOT_SECRET'] = 'seg'
    c = app.test_client()
    # conversa open sem código: ignorada como sempre (nao-pending)
    r = c.post('/crm/bot?k=seg',
               json=_payload_webhook('oi, tudo bem?', status='open'))
    assert r.get_json().get('ignorado') == 'nao-pending'

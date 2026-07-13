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

# Referência à função REAL, capturada ANTES da fixture autouse patchear o
# atributo do módulo (os testes de fail-open/NXDOMAIN exercitam ela).
from app.services.wifi_portal import _dominio_email_resolve as _RESOLVE_REAL

# O gate da loja exige LOJA_VISIVEL=1 E host público (LOJA_HOSTS). O marker
# `loja_host` vai SÓ nos testes de rota /loja/wifi — no arquivo inteiro ele
# derrubaria o /crm/bot (em host de loja, só /loja/* responde — 404).


@pytest.fixture
def visivel(monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')


@pytest.fixture(autouse=True)
def _sem_dns(monkeypatch):
    """A checagem de domínio de e-mail (MX/A) não pode bater em DNS real
    nos testes — determinístico e offline. Casos negativos re-patcham."""
    from app.services import wifi_portal
    monkeypatch.setattr(wifi_portal, '_dominio_email_resolve',
                        lambda dominio: True)


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


def test_validar_form_exige_nome_e_sobrenome(app):
    from app.services import wifi_portal
    with app.app_context():
        _, erros = wifi_portal.validar_form(_form(nome='Maria'))
        assert erros == ['Informe nome e sobrenome.']
        _, erros = wifi_portal.validar_form(_form(nome='Maria da Silva'))
        assert not erros


def test_validar_form_whatsapp_celular_br(app):
    from app.services import wifi_portal
    with app.app_context():
        # fixo (sem o nono dígito 9) não recebe WhatsApp
        _, erros = wifi_portal.validar_form(_form(telefone='(11) 3888-7777'))
        assert len(erros) == 1 and 'WhatsApp' in erros[0]
        # DDD que não existe (20) = digitação errada
        _, erros = wifi_portal.validar_form(_form(telefone='20 98888-7777'))
        assert len(erros) == 1 and 'WhatsApp' in erros[0]
        # com o 55 do país, válido
        _, erros = wifi_portal.validar_form(
            _form(telefone='+55 11 98888-7777'))
        assert not erros


def test_validar_form_email_typo_de_provedor(app):
    from app.services import wifi_portal
    with app.app_context():
        _, erros = wifi_portal.validar_form(_form(email='maria@gmial.com'))
        assert erros == ['Confira o e-mail — você quis dizer @gmail.com?']
        _, erros = wifi_portal.validar_form(_form(email='maria@gmail.com'))
        assert not erros


def test_validar_form_email_dominio_inexistente(app, monkeypatch):
    from app.services import wifi_portal
    with app.app_context():
        monkeypatch.setattr(wifi_portal, '_dominio_email_resolve',
                            lambda dominio: False)
        _, erros = wifi_portal.validar_form(_form())
        assert len(erros) == 1 and 'domínio' in erros[0]


def test_dns_indisponivel_nao_barra_cadastro(monkeypatch):
    """Fail-open de infra: resolver fora do ar ≠ domínio inexistente."""
    import dns.exception
    import dns.resolver

    def _estoura(self, *a, **kw):
        raise dns.exception.Timeout()
    monkeypatch.setattr(dns.resolver.Resolver, 'resolve', _estoura)
    assert _RESOLVE_REAL('qualquer-dominio.com.br') is True


def test_dns_nxdomain_reprova(monkeypatch):
    """Domínio que NÃO existe (NXDOMAIN) reprova o e-mail."""
    import dns.resolver

    def _nx(self, *a, **kw):
        raise dns.resolver.NXDOMAIN()
    monkeypatch.setattr(dns.resolver.Resolver, 'resolve', _nx)
    assert _RESOLVE_REAL('nao-existe-mesmo.com.br') is False


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

@pytest.mark.loja_host
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


@pytest.mark.loja_host
@pytest.mark.loja_host
def test_wifi_criar_conta_nova(app, visivel):
    from app.models import Cliente
    from app.services import wifi_portal
    with app.app_context():
        dados, erros = wifi_portal.validar_form(_form(
            email='novo@example.com', telefone='(11) 98888-7777'))
        assert not erros
        status, c = wifi_portal.criar_conta_direta(dados)
        assert status == 'criada'
        assert c.tem_conta and c.check_senha('segredo1')
        assert c.aniversario_dia == 15 and c.aniversario_mes == 3
        # e-mail já com conta → 'ja_existe' (não sobrescreve senha)
        dados2, _ = wifi_portal.validar_form(_form(
            email='novo@example.com', senha='outrasenha9'))
        status2, _c2 = wifi_portal.criar_conta_direta(dados2)
        assert status2 == 'ja_existe'
        assert Cliente.query.filter_by(email='novo@example.com').one() \
            .check_senha('segredo1')   # senha ANTIGA intacta


def test_wifi_criar_email_existente_nao_reivindica(app, visivel):
    """Privacidade (caso 'esposa ciumenta'): e-mail que já existe — conta OU
    convidado — NÃO é criado/reivindicado nem recebe e-mail. Nada do histórico
    do dono do e-mail é exposto a um terceiro que só sabe o e-mail."""
    from app.models import Cliente
    from app.services import wifi_portal
    with app.app_context():
        # convidado (sem senha) com histórico
        g = Cliente(nome='Guest', email='guest@example.com', telefone='x')
        db.session.add(g)
        db.session.commit()
        dados, _ = wifi_portal.validar_form(_form(email='guest@example.com'))
        with patch('app.services.loja_auth.iniciar_verificacao_cadastro') \
                as ver:
            status, c = wifi_portal.criar_conta_direta(dados)
        assert status == 'ja_existe' and c is None
        assert not ver.called                 # NÃO manda e-mail
        # o convidado continua SEM senha (não foi reivindicado)
        assert not Cliente.query.filter_by(
            email='guest@example.com').one().tem_conta


@pytest.mark.loja_host
def test_rota_wifi_criar_web(app, visivel):
    c = app.test_client()
    r = c.get('/loja/wifi/criar')
    assert r.status_code == 200 and 'criar conta' in r.get_data(
        as_text=True).lower()
    r2 = c.post('/loja/wifi/criar',
                data=_form(email='web@example.com',
                           telefone='(11) 98888-7777'),
                follow_redirects=True)
    assert r2.status_code == 200 and 'Conta criada' in r2.get_data(
        as_text=True)


@pytest.mark.loja_host
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


# ── Diagnóstico do Omada (fase 2) ────────────────────────────────────────

def _login_owner(c, owner_user):
    with c.session_transaction() as sess:
        sess['_user_id'] = str(owner_user.id)
        sess['_fresh'] = True


def test_debug_omada_exige_owner(app):
    c = app.test_client()
    r = c.get('/admin/debug-omada')
    assert r.status_code in (302, 401, 403)   # sem login não entra


def test_debug_omada_sem_envs(app, owner_user):
    c = app.test_client()
    _login_owner(c, owner_user)
    r = c.get('/admin/debug-omada')
    assert r.status_code == 200
    d = r.get_json()
    assert d['configurado'] is False
    assert 'OMADA_CLIENT_ID' in d['envs']
    assert not d['envs']['OMADA_CLIENT_ID']['presente']
    assert 'OMADA_*' in d['conclusao']


def test_debug_omada_com_envs_e_mac_de_teste(app, owner_user):
    with app.app_context():
        for k in ('OMADA_API_URL', 'OMADA_CLIENT_ID', 'OMADA_CLIENT_SECRET',
                  'OMADA_OMADAC_ID', 'OMADA_SITE_ID'):
            app.config[k] = 'x' * 8
    c = app.test_client()
    _login_owner(c, owner_user)
    with patch('app.services.omada._token', return_value='tok'), \
            patch('app.services.omada.listar_sites',
                  return_value=[{'id': 's1', 'nome': 'ribeiro do vale'}]), \
            patch('app.services.omada.autorizar_cliente',
                  return_value={'ok': True, 'erro': None}) as aut:
        r = c.get('/admin/debug-omada?autorizar_mac=AA:BB:CC:11:22:33')
    d = r.get_json()
    assert d['configurado'] is True and d['token'] == 'ok'
    assert d['sites'] == [{'id': 's1', 'nome': 'ribeiro do vale'}]
    assert d['autorizacao_teste'] == {'ok': True, 'erro': None}
    assert aut.call_args[0][0] == 'AA:BB:CC:11:22:33'


# ── Vouchers (trava dura sem API) ────────────────────────────────────────

def test_importar_vouchers_formatos_e_dedup(app):
    from app.services import wifi_portal
    with app.app_context():
        csv = ('Code,Note,Duration\n'
            '"84729183",lote1,480\n'
            '1927 3645,lote1,480\n'
            '84729183,repetido,480\n'
            'abc,linha-invalida,x\n')
        imp, dup, ign = wifi_portal.importar_vouchers(csv, 'lote-teste')
        assert (imp, dup, ign) == (2, 1, 2)   # header + inválida ignoradas
        # segunda importação do mesmo arquivo: tudo duplicado
        imp2, dup2, _ = wifi_portal.importar_vouchers(csv, 'lote-teste')
        assert imp2 == 0 and dup2 == 3
        assert wifi_portal.vouchers_restantes() == 2


def test_voucher_vai_na_resposta_do_whatsapp(app):
    from app.models import WifiVoucher
    from app.services import wifi_portal
    with app.app_context():
        wifi_portal.importar_vouchers('84729183\n', 'lote')
        s = _sessao()
        res = wifi_portal.processar_codigo_whatsapp(s.codigo, '11988887777')
        assert '84729183' in res['texto']
        v = WifiVoucher.query.filter_by(codigo='84729183').one()
        assert v.usado_em is not None and v.sessao_id == s.id
        assert wifi_portal.vouchers_restantes() == 0


def test_sem_voucher_fluxo_segue_sem_mencao(app):
    """Estoque vazio (pré-enforcement): a resposta não fala de código."""
    from app.services import wifi_portal
    with app.app_context():
        s = _sessao()
        res = wifi_portal.processar_codigo_whatsapp(s.codigo, '11988887777')
        assert res['sessao'].resultado == 'conta_criada'
        assert 'Código do Wi-Fi' not in res['texto']


def test_aviso_estoque_baixo_com_dedup(app, monkeypatch):
    from app.services import wifi_portal
    with app.app_context():
        app.config['WIFI_VOUCHER_AVISO_MIN'] = 5
        app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999990000'
        wifi_portal.importar_vouchers('11112222\n', 'lote')
        s = _sessao()
        with patch('app.services.zapi.enviar_texto') as tx:
            wifi_portal.processar_codigo_whatsapp(s.codigo, '11988887777')
        assert tx.called
        assert 'vouchers' in tx.call_args[0][1]
        # dedup 24h: segunda validação não re-manda
        wifi_portal.importar_vouchers('33334444\n', 'lote')
        s2 = _sessao(email='outra@example.com', telefone='(11) 97777-6666')
        with patch('app.services.zapi.enviar_texto') as tx2:
            wifi_portal.processar_codigo_whatsapp(s2.codigo, '11977776666')
        assert not tx2.called


def test_rota_admin_vouchers_exige_owner(app):
    # Teste SEPARADO do caso logado: sob o app context do conftest, o `g`
    # do Flask é compartilhado entre requests do MESMO teste, e a request
    # anônima deixa `g._login_user` anônimo em cache — a logada seguinte
    # herdaria e daria 403 falso.
    assert app.test_client().get('/admin/wifi-vouchers').status_code \
        in (302, 401, 403)


def test_rota_admin_vouchers_owner(app, owner_user):
    c = app.test_client()
    _login_owner(c, owner_user)
    r = c.get('/admin/wifi-vouchers')
    assert r.status_code == 200
    r2 = c.post('/admin/wifi-vouchers',
                data={'vouchers': '55556666\n77778888', 'lote': 'manual'})
    body = r2.get_data(as_text=True)
    assert r2.status_code == 200 and 'Importados: <strong>2</strong>' in body


def test_debug_omada_sem_site_id_lista_sites(app, owner_user):
    """As 4 envs do token bastam pra listar os sites — o id do site sai
    da própria resposta (é o jeito de descobrir o OMADA_SITE_ID)."""
    with app.app_context():
        for k in ('OMADA_API_URL', 'OMADA_CLIENT_ID', 'OMADA_CLIENT_SECRET',
                  'OMADA_OMADAC_ID'):
            app.config[k] = 'x' * 8
        app.config['OMADA_SITE_ID'] = ''
    c = app.test_client()
    _login_owner(c, owner_user)
    with patch('app.services.omada._token', return_value='tok'), \
            patch('app.services.omada.listar_sites',
                  return_value=[{'id': 's1', 'nome': 'ribeiro do vale'}]):
        r = c.get('/admin/debug-omada')
    d = r.get_json()
    assert d['configurado'] is False
    assert d['sites'] == [{'id': 's1', 'nome': 'ribeiro do vale'}]
    assert 'OMADA_SITE_ID' in d['conclusao']

"""Diagnostico do Chatwoot (/admin/debug-chatwoot + chatwoot.diagnostico).

Criado no incidente de 12/06/2026: WhatsApp "Falha ao enviar" + Instagram
"400 Session Invalid" + app dos atendentes com "unexpected error". A rota
roda as checagens DO SERVIDOR de prod e conclui sozinha qual familia de
problema e — hospedagem, token nosso, ou canais Meta."""
from unittest.mock import patch


class _Resp:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body
        # Fiel ao requests real: resposta com JSON tem .text preenchido
        # (o codigo de prod usa o padrao `r.json() if r.text else {}`).
        if text is None:
            text = 'x' if body is not None else ''
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError('no json')
        return self._body


def _cfg(app, **extra):
    app.config['CHATWOOT_URL'] = 'https://chat.exemplo.com.br'
    app.config['CHATWOOT_ACCOUNT_ID'] = '1'
    app.config['CHATWOOT_API_TOKEN'] = 'tok-api'
    app.config['CHATWOOT_BOT_TOKEN'] = 'tok-bot'
    for k, v in extra.items():
        app.config[k] = v


def test_sem_url_configura_conclusao_de_env(app):
    from app.services import chatwoot
    app.config['CHATWOOT_URL'] = ''
    out = chatwoot.diagnostico()
    assert 'CHATWOOT_URL vazia' in out['conclusao']


def test_servidor_fora_conclui_hospedagem(app):
    import requests as req

    from app.services import chatwoot
    _cfg(app)
    with patch('app.services.chatwoot.requests.get',
               side_effect=req.ConnectionError('refused')):
        out = chatwoot.diagnostico()
    assert out['servidor_http'] is None
    assert 'hospedagem' in out['conclusao']
    assert 'Railway do Chatwoot' in out['conclusao']


def test_5xx_conclui_servidor_doente(app):
    from app.services import chatwoot
    _cfg(app)
    with patch('app.services.chatwoot.requests.get',
               return_value=_Resp(500, text='oops')):
        out = chatwoot.diagnostico()
    assert '5xx' in out['conclusao']
    assert 'Sidekiq' in out['conclusao']


def test_token_401_conclui_token_invalido(app):
    from app.services import chatwoot
    _cfg(app)

    def fake_get(url, **kw):
        if url.endswith('/api'):
            return _Resp(200, body={'version': '3.12.0'})
        return _Resp(401, body={'error': 'unauthorized'})

    with patch('app.services.chatwoot.requests.get', side_effect=fake_get):
        out = chatwoot.diagnostico()
    assert out['servidor_http'] == 200
    assert out['servidor_versao'] == '3.12.0'
    assert out['api_token_http'] == 401
    assert '401' in out['conclusao']
    assert 'token DESTE sistema' in out['conclusao']


def test_inbox_quebrado_aponta_canal_pelo_nome(app):
    """Com token de usuario valido, o diagnostico le /inboxes e aponta
    EXATAMENTE qual canal precisa de Reauthorize (campo
    `reauthorization_required` do Chatwoot — e o '400 Session Invalid'
    do incidente de 12/06/2026)."""
    from app.services import chatwoot
    _cfg(app)

    def fake_get(url, **kw):
        if url.endswith('/api'):
            return _Resp(200, body={'version': '4.14.1'})
        if url.endswith('/inboxes'):
            return _Resp(200, body={'payload': [
                {'name': 'Instagram O PAO', 'channel_type': 'Channel::Instagram',
                 'reauthorization_required': True},
                {'name': 'O PAO WhatsApp', 'channel_type': 'Channel::Whatsapp',
                 'reauthorization_required': False},
            ]})
        return _Resp(200, body={'payload': []})

    with patch('app.services.chatwoot.requests.get', side_effect=fake_get):
        out = chatwoot.diagnostico()
    assert out['inboxes'][0]['precisa_reautorizar'] is True
    assert 'Instagram O PAO' in out['conclusao']
    assert 'Reauthorize' in out['conclusao']
    assert 'System User' in out['conclusao']


def test_tudo_ok_aponta_janela_24h_e_sidekiq(app):
    """Servidor, tokens e inboxes todos OK → as causas restantes de
    'Falha ao enviar' sao operacionais (janela de 24h da Meta) ou o
    worker do Chatwoot — a conclusao orienta os dois."""
    from app.services import chatwoot
    _cfg(app)

    def fake_get(url, **kw):
        if url.endswith('/api'):
            return _Resp(200, body={'version': '4.14.1'})
        if url.endswith('/inboxes'):
            return _Resp(200, body={'payload': [
                {'name': 'O PAO WhatsApp', 'channel_type': 'Channel::Whatsapp',
                 'reauthorization_required': False},
            ]})
        return _Resp(200, body={'payload': []})

    with patch('app.services.chatwoot.requests.get', side_effect=fake_get):
        out = chatwoot.diagnostico()
    assert 'janela de 24h' in out['conclusao']
    assert 'Sidekiq' in out['conclusao']


def test_diagnostico_nao_vaza_tokens(app):
    from app.services import chatwoot
    _cfg(app)

    def fake_get(url, **kw):
        return _Resp(200, body={'version': '3.12.0', 'payload': []})

    with patch('app.services.chatwoot.requests.get', side_effect=fake_get):
        out = chatwoot.diagnostico()
    blob = str(out)
    assert 'tok-api' not in blob
    assert 'tok-bot' not in blob
    # Forense sem vazamento: tamanhos e flag de URL aparecem
    assert out['api_token_len'] == len('tok-api')
    assert out['bot_token_len'] == len('tok-bot')
    assert out['bot_token_parece_url'] is False


def test_diagnostico_detecta_url_colada_no_lugar_do_token(app):
    """Caso real (12/06/2026): bot_token 401 mesmo apos o dono 'ja ter
    colado'. Causa comum: colar a Outgoing URL do Agent Bot em vez do
    Access Token. O flag desmascara sem expor o valor."""
    from app.services import chatwoot
    _cfg(app, CHATWOOT_BOT_TOKEN='https://gestao.exemplo.com/crm/bot?k=x')

    def fake_get(url, **kw):
        return _Resp(200, body={'version': '4.14.1', 'payload': []})

    with patch('app.services.chatwoot.requests.get', side_effect=fake_get):
        out = chatwoot.diagnostico()
    assert out['bot_token_parece_url'] is True


def test_erros_de_envio_lista_falhas_com_erro_do_canal(app):
    """Cada mensagem que falhou no Chatwoot carrega o erro bruto do canal
    em content_attributes.external_error — ex: janela de 24h da Meta vs
    token morto. E o discriminador do 'Falha ao enviar' sem precisar
    clicar no ⚠️ do app."""
    from app.services import chatwoot
    _cfg(app)

    def fake_get(url, **kw):
        assert url.endswith('/conversations/176/messages')
        return _Resp(200, body={'payload': [
            {'content': 'Boa tarde', 'created_at': 2, 'status': 'failed',
             'message_type': 1,
             'content_attributes': {'external_error':
                 '131047: Re-engagement message — fora da janela de 24h'}},
            {'content': 'oi', 'created_at': 1, 'status': 'sent',
             'message_type': 0, 'content_attributes': {}},
        ]})

    with patch('app.services.chatwoot.requests.get', side_effect=fake_get):
        out = chatwoot.erros_de_envio(176)
    assert out['ok'] is True
    assert out['qtd_falhas'] == 1
    assert out['falhas'][0]['mensagem'] == 'Boa tarde'
    assert '131047' in out['falhas'][0]['erro_canal']


def test_debug_chatwoot_aceita_param_conversa(app):
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        dono = Usuario(nome='dono', login='dono_cw2', papel='admin',
                       is_owner=True)
        dono.set_senha('senha123')
        db.session.add(dono)
        db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'dono_cw2', 'senha': 'senha123'})
    app.config['CHATWOOT_URL'] = ''   # diagnostico curto; so o shape importa
    r = c.get('/admin/debug-chatwoot?conversa=176')
    assert r.status_code == 200
    data = r.get_json()
    # Tras erros (Meta) E historico (o que o BOT disse) — fecha tanto
    # "Falha ao enviar" quanto "bot delirou?".
    assert 'erros_da_conversa_176' in data
    assert 'historico_da_conversa_176' in data


def test_debug_bot_compara_vnda_com_estoque_loja(app):
    """Caso real (12/06/2026): vigia alertou 'bot disse esgotado mas tem
    872 un'. Bot consulta VNDA; vigia compara contra EstoqueLoja. Esta
    rota mostra as duas fontes lado a lado pra qualquer produto."""
    from app.extensions import db
    from app.models import EstoqueLoja, Loja, Receita, Usuario
    with app.app_context():
        loja = Loja(nome='Loja A', ativa=True)
        rec = Receita(nome='Pain au Chocolat', categoria='Paes',
                      rendimento_qtd=1, rendimento_unidade='un',
                      peso_base=80.0)
        db.session.add_all([loja, rec])
        db.session.flush()
        db.session.add(EstoqueLoja(loja_id=loja.id, receita_id=rec.id,
                                    quantidade=872))
        dono = Usuario(nome='dono', login='dono_bot', papel='admin',
                       is_owner=True)
        dono.set_senha('senha123')
        db.session.add(dono)
        db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'dono_bot', 'senha': 'senha123'})

    # VNDA mockado pra retornar 'available: false' (= o que o bot viu)
    fake_vnda = {'produtos': [{'nome': 'Pain au Chocolat', 'sku': 'PAC1',
                                'preco': 12.0, 'disponivel': False}]}
    from unittest.mock import patch
    with patch('app.services.bot_tools.consultar_produtos',
               return_value=fake_vnda):
        r = c.get('/admin/debug-bot?busca=Pain au Chocolat')
    assert r.status_code == 200
    data = r.get_json()
    # Lado VNDA: o que o bot viu (disponivel=False explica "esgotado")
    assert data['vnda']['produtos'][0]['disponivel'] is False
    # Lado EstoqueLoja: o que o vigia vê (872 un, justificando o alerta)
    estoq = data['estoque_loja']
    assert any(e['nome'] == 'Pain au Chocolat' and e['qtd_total'] == 872
               for e in estoq)
    # Por loja: 'Loja A' tem as 872
    pac = next(e for e in estoq if e['nome'] == 'Pain au Chocolat')
    assert pac['por_loja']['Loja A'] == 872


def test_debug_bot_sem_param_da_400(app):
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        dono = Usuario(nome='dono', login='dono_bot2', papel='admin',
                       is_owner=True)
        dono.set_senha('senha123')
        db.session.add(dono)
        db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'dono_bot2', 'senha': 'senha123'})
    r = c.get('/admin/debug-bot')
    assert r.status_code == 400


def test_debug_bot_nega_admin_comum(app):
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        comum = Usuario(nome='adm', login='adm_bot', papel='admin',
                        is_owner=False)
        comum.set_senha('senha123')
        db.session.add(comum)
        db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'adm_bot', 'senha': 'senha123'})
    assert c.get('/admin/debug-bot?busca=x').status_code == 403


# ── Vigia de infra (cron 15min → alerta WhatsApp do dono) ───────────────


def _reset_vigia():
    """Estado do vigia agora vive no banco (AppConfig). O conftest ja
    limpa todas as tabelas entre testes — esta funcao existe so como
    ponto de extensao caso um teste precise resetar no MEIO da sua
    execucao (ex: simular 2 chamadas em janelas separadas)."""
    from app.extensions import db
    from app.models import AppConfig
    AppConfig.query.filter(
        AppConfig.key.like('vigia_chatwoot_%')
    ).delete(synchronize_session=False)
    db.session.commit()


def test_vigia_alerta_na_transicao_e_throttla_repeticao(app):
    from app.services import chatwoot
    _cfg(app)
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999990000'
    _reset_vigia()
    doente = {'saudavel': False, 'conclusao': 'Canal(is) com token Meta morto'}
    with patch('app.services.chatwoot.diagnostico', return_value=doente), \
         patch('app.services.zapi.enviar_texto') as envia:
        r1 = chatwoot.vigiar_infra()
        r2 = chatwoot.vigiar_infra()
    assert r1 == {'rodou': True, 'enviado': True, 'tipo': 'alerta'}
    assert r2['tipo'] == 'throttle'          # mesmo problema, sem re-spam
    assert envia.call_count == 1
    msg = envia.call_args[0][1]
    assert 'token Meta morto' in msg
    assert '/admin/debug-chatwoot' in msg


def test_vigia_avisa_recuperacao_uma_vez(app):
    from app.services import chatwoot
    _cfg(app)
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999990000'
    _reset_vigia()
    doente = {'saudavel': False, 'conclusao': 'x'}
    saud = {'saudavel': True, 'conclusao': 'ok'}
    with patch('app.services.zapi.enviar_texto') as envia:
        with patch('app.services.chatwoot.diagnostico', return_value=doente):
            chatwoot.vigiar_infra()
        with patch('app.services.chatwoot.diagnostico', return_value=saud):
            r2 = chatwoot.vigiar_infra()
            r3 = chatwoot.vigiar_infra()
    assert r2['tipo'] == 'recuperacao'
    assert r3 == {'rodou': True, 'enviado': False, 'tipo': 'saudavel'}
    assert envia.call_count == 2             # 1 alerta + 1 normalizou
    assert 'normalizou' in envia.call_args[0][1]


def test_vigia_persiste_estado_entre_processos(app):
    """Bug real (12/06/2026): o estado anti-spam vivia in-memory. Cada
    deploy/restart zerava o estado e a proxima execucao do cron re-
    alertava o MESMO problema. Resultado: o dono recebeu 2x o mesmo
    aviso em segundos durante uma janela de Apply seguidas.
    O estado agora vive em AppConfig — sobrevive a restart."""
    from app.services import chatwoot
    _cfg(app)
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999990000'
    doente = {'saudavel': False, 'conclusao': 'token 401'}
    with patch('app.services.chatwoot.diagnostico', return_value=doente), \
         patch('app.services.zapi.enviar_texto') as envia:
        chatwoot.vigiar_infra()        # 1o alerta, persiste no banco
        # Simula restart limpando QUALQUER cache em memoria do servico
        # (a fonte da verdade tem que ser o banco — sem isso o teste
        # seria fraco). Hoje nao ha cache em memoria; defensivo.
        for nome in dir(chatwoot):
            if nome.startswith('_vigia_infra_estado'):
                getattr(chatwoot, nome).clear()
        chatwoot.vigiar_infra()        # depois de "restart"
        chatwoot.vigiar_infra()
    assert envia.call_count == 1       # NAO realertou


def test_vigia_problema_diferente_realerta_sem_esperar_6h(app):
    from app.services import chatwoot
    _cfg(app)
    app.config['ZAPI_BOT_DONO_NUMERO'] = '5511999990000'
    _reset_vigia()
    with patch('app.services.zapi.enviar_texto') as envia:
        with patch('app.services.chatwoot.diagnostico',
                   return_value={'saudavel': False, 'conclusao': 'problema A'}):
            chatwoot.vigiar_infra()
        with patch('app.services.chatwoot.diagnostico',
                   return_value={'saudavel': False, 'conclusao': 'problema B'}):
            r = chatwoot.vigiar_infra()
    assert r['tipo'] == 'alerta'
    assert envia.call_count == 2


def test_vigia_sem_config_nao_roda(app):
    from app.services import chatwoot
    _reset_vigia()
    app.config['CHATWOOT_URL'] = ''
    assert chatwoot.vigiar_infra()['rodou'] is False
    _cfg(app)
    app.config['ZAPI_BOT_DONO_NUMERO'] = ''
    r = chatwoot.vigiar_infra()
    assert r['rodou'] is False
    assert 'DONO' in r['motivo']


def test_diagnostico_seta_flag_saudavel(app):
    """O vigia decide pela flag `saudavel` (maquina), nao pelo texto."""
    from app.services import chatwoot
    _cfg(app)

    def ok_get(url, **kw):
        if url.endswith('/api'):
            return _Resp(200, body={'version': '4.14.1'})
        if url.endswith('/inboxes'):
            return _Resp(200, body={'payload': [
                {'name': 'WA', 'channel_type': 'Channel::Whatsapp',
                 'reauthorization_required': False}]})
        return _Resp(200, body={'payload': []})

    with patch('app.services.chatwoot.requests.get', side_effect=ok_get):
        assert chatwoot.diagnostico()['saudavel'] is True

    def get_401(url, **kw):
        if url.endswith('/api'):
            return _Resp(200, body={'version': '4.14.1'})
        return _Resp(401, body={'error': 'unauthorized'})

    with patch('app.services.chatwoot.requests.get', side_effect=get_401):
        assert chatwoot.diagnostico()['saudavel'] is False


def test_rota_nega_admin_comum(app):
    """Admin nao-owner leva 403 (padrao owner_required — mesmo desenho
    dos outros /admin/debug-*)."""
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        comum = Usuario(nome='adm', login='adm_cw', papel='admin',
                        is_owner=False)
        comum.set_senha('senha123')
        db.session.add(comum)
        db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'adm_cw', 'senha': 'senha123'})
    assert c.get('/admin/debug-chatwoot').status_code == 403


def test_rota_responde_pro_owner(app):
    from app.extensions import db
    from app.models import Usuario
    with app.app_context():
        dono = Usuario(nome='dono', login='dono_cw', papel='admin',
                       is_owner=True)
        dono.set_senha('senha123')
        db.session.add(dono)
        db.session.commit()
    c = app.test_client()
    c.post('/auth/login', data={'login': 'dono_cw', 'senha': 'senha123'})
    app.config['CHATWOOT_URL'] = ''
    r = c.get('/admin/debug-chatwoot')
    assert r.status_code == 200
    assert 'conclusao' in r.get_json()

"""Tela /entregas/painel-testes: painel de pedidos (iframe same-origin) +
atendimento NOSSO (conversas do Chatwoot via API, SEM iframe) + klaxon de
nova conversa. Read-only.

O embed direto do dashboard do Chatwoot foi abandonado (o servidor dele recusa
ser embutido); trazemos as conversas via fetch das rotas /entregas/api/
atendimento/*.
"""


def _staff(app):
    from app.extensions import db
    from app.models import Usuario
    u = Usuario(nome='Dono', login='dono_pt', papel='admin', is_owner=True)
    u.set_senha('x' * 8)
    db.session.add(u)
    db.session.commit()
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(u.id)
        s['_fresh'] = True
    return c


# ── Render da tela ──


def test_painel_testes_renderiza(app):
    c = _staff(app)
    r = c.get('/entregas/painel-testes')
    assert r.status_code == 200
    html = r.data.decode()
    assert '/entregas/painel?embed=1' in html              # iframe do painel real
    assert 'id="divisor"' in html                          # divisor arrastavel
    assert 'id="at-lista"' in html                         # widget de atendimento
    assert '/entregas/api/atendimento/conversas' in html   # fetch da lista


def test_painel_testes_csp_frame_self(app):
    c = _staff(app)
    r = c.get('/entregas/painel-testes')
    csp = r.headers.get('Content-Security-Policy', '')
    # so o painel embutido; o Chatwoot NAO entra no frame-src (sem iframe dele)
    assert "frame-src 'self';" in csp


def test_painel_testes_csp_img_libera_chatwoot(app):
    """Dominio do Chatwoot vai pro img-src (anexos de imagem dos clientes na
    thread), NUNCA pro frame-src."""
    app.config['CHATWOOT_URL'] = 'https://atendimento.exemplo.com'
    c = _staff(app)
    r = c.get('/entregas/painel-testes')
    csp = r.headers.get('Content-Security-Policy', '')
    assert 'https://atendimento.exemplo.com' in csp
    frame_part = csp.split('frame-src', 1)[1] if 'frame-src' in csp else ''
    assert 'atendimento.exemplo.com' not in frame_part


def test_painel_embed_permite_same_origin(app):
    c = _staff(app)
    r = c.get('/entregas/painel?embed=1')
    assert r.status_code == 200
    assert r.headers.get('X-Frame-Options') == 'SAMEORIGIN'


def test_painel_producao_sem_embed_continua_deny(app):
    """REGRESSAO: painel de producao (sem ?embed) segue X-Frame-Options DENY."""
    c = _staff(app)
    r = c.get('/entregas/painel')
    assert r.status_code == 200
    assert r.headers.get('X-Frame-Options') == 'DENY'


def test_painel_testes_exige_login(app):
    c = app.test_client()
    r = c.get('/entregas/painel-testes')
    assert r.status_code in (301, 302)
    assert '/login' in r.headers.get('Location', '')


def test_painel_testes_botao_alertas(app):
    c = _staff(app)
    html = c.get('/entregas/painel-testes').data.decode()
    assert 'LIGAR ALERTAS' in html
    assert '/entregas/api/painel-testes/chatwoot-pending' in html
    assert 'klaxon' in html


# ── Backend: chatwoot.listar_conversas ──


def test_listar_conversas_parseia(app, monkeypatch):
    from app.services import chatwoot

    class _Resp:
        status_code = 200
        text = '{}'

        def json(self):
            return {'data': {'payload': [{
                'id': 7, 'status': 'open',
                'meta': {'sender': {'name': 'Caio'}, 'channel': 'Channel::WebWidget'},
                'last_non_activity_message': {'content': 'Oi, tem cesta?'},
                'last_activity_at': 1000, 'unread_count': 2,
            }]}}

    monkeypatch.setattr(chatwoot, 'disponivel', lambda: True)
    monkeypatch.setattr(chatwoot, '_headers', lambda: {})
    monkeypatch.setattr(chatwoot.requests, 'get', lambda *a, **k: _Resp())
    with app.app_context():
        cs = chatwoot.listar_conversas(status='open')
    assert len(cs) == 1
    assert cs[0]['contato'] == 'Caio'
    assert cs[0]['preview'] == 'Oi, tem cesta?'
    assert cs[0]['nao_lidas'] == 2
    assert cs[0]['status'] == 'open'


def test_listar_conversas_indisponivel_vazio(app, monkeypatch):
    from app.services import chatwoot
    monkeypatch.setattr(chatwoot, 'disponivel', lambda: False)
    monkeypatch.setattr(chatwoot, 'bot_disponivel', lambda: False)
    with app.app_context():
        assert chatwoot.listar_conversas() == []


# ── Rotas de atendimento ──


def test_api_conversas(app, monkeypatch):
    from app.services import chatwoot
    monkeypatch.setattr(
        chatwoot, 'listar_conversas',
        lambda **k: [{'id': 1, 'contato': 'Ana', 'preview': 'oi',
                      'status': 'open', 'ultima_em': 1, 'nao_lidas': 0, 'canal': ''}])
    c = _staff(app)
    r = c.get('/entregas/api/atendimento/conversas?status=open')
    assert r.status_code == 200
    assert r.get_json()['conversas'][0]['contato'] == 'Ana'


def test_api_conversa_thread(app, monkeypatch):
    from app.services import chatwoot
    monkeypatch.setattr(
        chatwoot, 'buscar_historico',
        lambda cid, **k: [{'role': 'user', 'content': 'tem cesta?'},
                          {'role': 'assistant', 'content': 'temos sim!'}])
    c = _staff(app)
    r = c.get('/entregas/api/atendimento/conversa/7')
    assert r.status_code == 200
    j = r.get_json()
    assert j['id'] == 7
    assert [m['content'] for m in j['mensagens']] == ['tem cesta?', 'temos sim!']


def test_debug_owner_mostra_contagem_por_status(app, monkeypatch):
    """Debug owner-only: lista quantas conversas vem em cada status."""
    from app.services import chatwoot
    chamadas = []

    def fake_listar(**k):
        chamadas.append(k.get('status'))
        return [{'id': 1, 'contato': 'x', 'preview': '', 'status': k.get('status'),
                 'ultima_em': 0, 'nao_lidas': 0, 'canal': ''}] * 3

    monkeypatch.setattr(chatwoot, 'listar_conversas', fake_listar)
    monkeypatch.setattr(chatwoot, 'disponivel', lambda: True)
    monkeypatch.setattr(chatwoot, 'bot_disponivel', lambda: True)
    c = _staff(app)
    r = c.get('/entregas/api/atendimento/debug')
    assert r.status_code == 200
    j = r.get_json()
    assert j['token_usuario_ok'] is True
    assert set(chamadas) == {'open', 'pending', 'resolved'}
    assert j['por_status']['open'] == 3


def test_debug_exige_owner(app, admin_user):
    """Admin nao-owner nao acessa o debug (dado de cliente)."""
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(admin_user.id)
        s['_fresh'] = True
    assert c.get('/entregas/api/atendimento/debug').status_code == 403


def _csrf(client):
    """Pega um CSRF token valido pegando um GET autenticado primeiro."""
    client.get('/entregas/painel-testes')
    with client.session_transaction() as s:
        # Flask-WTF guarda o secret na sessao; gerar o token via app
        from flask import current_app
        with current_app.test_request_context():
            from flask_wtf.csrf import generate_csrf
            return generate_csrf()


# ── POST /enviar ──


def test_enviar_chama_servico_painel(app, monkeypatch):
    """Envia chama enviar_mensagem_painel (token Painel), NAO o bot."""
    from app.services import chatwoot
    chamadas = []
    monkeypatch.setattr(chatwoot, 'enviar_mensagem_painel',
                        lambda cid, content: chamadas.append((cid, content))
                        or {'ok': True})
    monkeypatch.setattr(chatwoot, 'enviar_mensagem',
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError('NAO deve usar bot enviar_mensagem')))
    c = _staff(app)
    r = c.post('/entregas/api/atendimento/conversa/42/enviar',
               json={'content': 'oi tudo bem'})
    assert r.status_code == 200
    assert r.get_json()['ok'] is True
    assert chamadas == [(42, 'oi tudo bem')]


def test_enviar_vazio_400(app, monkeypatch):
    from app.services import chatwoot
    monkeypatch.setattr(chatwoot, 'enviar_mensagem_painel',
                        lambda cid, content: {'ok': True})
    c = _staff(app)
    r = c.post('/entregas/api/atendimento/conversa/1/enviar', json={'content': '  '})
    assert r.status_code == 400
    assert r.get_json()['ok'] is False


def test_enviar_muito_longo_400(app, monkeypatch):
    from app.services import chatwoot
    monkeypatch.setattr(chatwoot, 'enviar_mensagem_painel',
                        lambda cid, content: {'ok': True})
    c = _staff(app)
    r = c.post('/entregas/api/atendimento/conversa/1/enviar',
               json={'content': 'x' * 5000})
    assert r.status_code == 400


def test_enviar_erro_propaga_502(app, monkeypatch):
    """Erro no Chatwoot devolve 502 + ok=false (UI mostra, nao silencia)."""
    from app.services import chatwoot
    monkeypatch.setattr(chatwoot, 'enviar_mensagem_painel',
                        lambda cid, content: {'ok': False, 'erro': 'HTTP 401'})
    c = _staff(app)
    r = c.post('/entregas/api/atendimento/conversa/1/enviar', json={'content': 'oi'})
    assert r.status_code == 502
    j = r.get_json()
    assert j['ok'] is False
    assert 'HTTP 401' in j['erro']


def test_enviar_exige_login(app):
    c = app.test_client()
    r = c.post('/entregas/api/atendimento/conversa/1/enviar', json={'content': 'oi'})
    assert r.status_code in (301, 302, 401)


# ── POST /status ──


def test_status_chama_definir_status(app, monkeypatch):
    from app.services import chatwoot
    chamadas = []
    monkeypatch.setattr(chatwoot, 'definir_status',
                        lambda cid, status, **k: chamadas.append((cid, status))
                        or {'ok': True})
    c = _staff(app)
    for st in ('open', 'pending', 'resolved'):
        r = c.post('/entregas/api/atendimento/conversa/7/status', json={'status': st})
        assert r.status_code == 200
    assert chamadas == [(7, 'open'), (7, 'pending'), (7, 'resolved')]


def test_status_invalido_400(app):
    c = _staff(app)
    r = c.post('/entregas/api/atendimento/conversa/1/status',
               json={'status': 'lixo'})
    assert r.status_code == 400


def test_status_erro_propaga(app, monkeypatch):
    from app.services import chatwoot
    monkeypatch.setattr(chatwoot, 'definir_status',
                        lambda cid, status, **k: {'ok': False, 'erro': 'HTTP 500'})
    c = _staff(app)
    r = c.post('/entregas/api/atendimento/conversa/1/status',
               json={'status': 'open'})
    assert r.status_code == 502


# ── buscar_historico devolve created_at ──


def test_buscar_historico_inclui_created_at(app, monkeypatch):
    """created_at (epoch UTC) vai no dict pra UI mostrar HH:MM em cada bolha.
    Callers antigos (bot/copilot/vigia) ignoram a chave extra."""
    from app.services import chatwoot

    class _R:
        status_code = 200
        text = '{}'

        def json(self):
            return {'payload': [
                {'message_type': 'incoming', 'content': 'oi',
                 'created_at': 1719417600},
                {'message_type': 'outgoing', 'content': 'olá',
                 'created_at': 1719417660},
                # ts invalido = None (nao quebra)
                {'message_type': 'incoming', 'content': 'q?',
                 'created_at': 'lixo'},
            ]}

    monkeypatch.setattr(chatwoot, 'disponivel', lambda: True)
    monkeypatch.setattr(chatwoot, '_headers', lambda: {})
    monkeypatch.setattr(chatwoot.requests, 'get', lambda *a, **k: _R())
    with app.app_context():
        h = chatwoot.buscar_historico(7)
    assert h[0]['created_at'] == 1719417600
    assert h[1]['created_at'] == 1719417660
    assert h[2]['created_at'] is None


# ── enviar_mensagem_painel (servico) ──


def test_enviar_mensagem_painel_sem_token_nao_envia(app, monkeypatch):
    """REGRESSAO: sem CHATWOOT_PAINEL_TOKEN NAO pode usar o bot como fallback
    (UI tem que falhar, nao confundir autor da mensagem)."""
    from app.services import chatwoot
    app.config.pop('CHATWOOT_PAINEL_TOKEN', None)
    posted = []
    monkeypatch.setattr(chatwoot.requests, 'post',
                        lambda *a, **k: posted.append(a) or None)
    with app.app_context():
        r = chatwoot.enviar_mensagem_painel(1, 'oi')
    assert r['ok'] is False
    assert posted == []


def test_enviar_mensagem_painel_usa_token_painel(app, monkeypatch):
    """O header api_access_token tem que ser o do Painel, nao o do bot."""
    from app.services import chatwoot
    app.config['CHATWOOT_URL'] = 'https://atendimento.x.com'
    app.config['CHATWOOT_ACCOUNT_ID'] = '1'
    app.config['CHATWOOT_PAINEL_TOKEN'] = 'tok-do-painel'
    app.config['CHATWOOT_BOT_TOKEN'] = 'tok-do-bot'
    capturado = {}

    class _R:
        status_code = 200
        text = ''

        def json(self):
            return {}

    def fake_post(url, json=None, headers=None, timeout=None):
        capturado['url'] = url
        capturado['headers'] = headers
        capturado['json'] = json
        return _R()

    monkeypatch.setattr(chatwoot.requests, 'post', fake_post)
    with app.app_context():
        r = chatwoot.enviar_mensagem_painel(99, 'mensagem real')
    assert r['ok'] is True
    assert capturado['headers']['api_access_token'] == 'tok-do-painel'
    assert capturado['json']['content'] == 'mensagem real'
    assert capturado['json']['message_type'] == 'outgoing'


def test_api_conversas_erro_nao_quebra(app, monkeypatch):
    from app.services import chatwoot

    def boom(**k):
        raise RuntimeError('chatwoot down')

    monkeypatch.setattr(chatwoot, 'listar_conversas', boom)
    c = _staff(app)
    r = c.get('/entregas/api/atendimento/conversas')
    assert r.status_code == 200
    assert r.get_json()['conversas'] == []


def test_api_atendimento_exige_login(app):
    c = app.test_client()
    assert c.get('/entregas/api/atendimento/conversas').status_code in (301, 302)
    assert c.get('/entregas/api/atendimento/conversa/1').status_code in (301, 302)


# ── API pending (klaxon) ──


def test_pending_api_retorna_ids(app, monkeypatch):
    from app.services import chatwoot
    monkeypatch.setattr(chatwoot, 'listar_conversas_paradas',
                        lambda **kw: [{'id': 198}, {'id': 201}])
    c = _staff(app)
    r = c.get('/entregas/api/painel-testes/chatwoot-pending')
    assert r.status_code == 200
    j = r.get_json()
    assert j['ids'] == [198, 201]
    assert j['count'] == 2


def test_pending_api_min_minutos_zero(app, monkeypatch):
    """REGRESSAO: precisa pegar conversas RECEM-criadas (0min), nao paradas."""
    chamadas = []
    from app.services import chatwoot
    monkeypatch.setattr(chatwoot, 'listar_conversas_paradas',
                        lambda **kw: (chamadas.append(kw) or []))
    c = _staff(app)
    c.get('/entregas/api/painel-testes/chatwoot-pending')
    assert chamadas[0].get('min_minutos') == 0
    assert chamadas[0].get('status') == 'pending'


def test_pending_api_erro_zero(app, monkeypatch):
    from app.services import chatwoot

    def boom(**kw):
        raise RuntimeError('down')

    monkeypatch.setattr(chatwoot, 'listar_conversas_paradas', boom)
    c = _staff(app)
    r = c.get('/entregas/api/painel-testes/chatwoot-pending')
    assert r.get_json() == {'ids': [], 'count': 0}

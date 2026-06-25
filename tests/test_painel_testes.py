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

"""Tela experimental /entregas/painel-testes: painel de entregas + Chatwoot
embutidos lado a lado (pedido do dono 25/06/2026).

Cobre: a rota renderiza os 2 iframes; a CSP libera frame-src pro Chatwoot
SO nesta rota; o painel embute same-origin via ?embed=1; e — trava de
regressao — o painel de PRODUCAO (sem ?embed) continua X-Frame-Options DENY.
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


def test_painel_testes_renderiza_dois_iframes(app):
    c = _staff(app)
    r = c.get('/entregas/painel-testes')
    assert r.status_code == 200
    html = r.data.decode()
    assert 'TESTE' in html
    # iframe do painel real, embutido same-origin com ?embed=1
    assert '/entregas/painel?embed=1' in html
    # divisor arrastavel presente
    assert 'id="divisor"' in html


def test_painel_testes_csp_libera_frame_self(app):
    c = _staff(app)
    r = c.get('/entregas/painel-testes')
    csp = r.headers.get('Content-Security-Policy', '')
    assert "frame-src 'self'" in csp


def test_painel_testes_csp_inclui_chatwoot_quando_configurado(app):
    app.config['CHATWOOT_URL'] = 'https://atendimento.exemplo.com'
    c = _staff(app)
    r = c.get('/entregas/painel-testes')
    csp = r.headers.get('Content-Security-Policy', '')
    assert 'https://atendimento.exemplo.com' in csp
    assert "frame-src 'self' https://atendimento.exemplo.com" in csp
    html = r.data.decode()
    # iframe + fallback "abrir em nova aba" (caso o Chatwoot recuse embed)
    assert 'https://atendimento.exemplo.com' in html
    assert 'target="_blank"' in html
    assert 'abrir em nova aba' in html
    # aviso oculto que aparece se o iframe nao carregar em 5s
    assert 'cw-aviso' in html


def test_painel_testes_sem_chatwoot_mostra_aviso(app):
    app.config.pop('CHATWOOT_URL', None)
    c = _staff(app)
    r = c.get('/entregas/painel-testes')
    html = r.data.decode()
    assert 'CHATWOOT_URL' in html  # aviso "nao configurado"
    csp = r.headers.get('Content-Security-Policy', '')
    # frame-src so com 'self' (nenhum dominio externo de frame)
    assert "frame-src 'self';" in csp


def test_painel_embed_permite_same_origin(app):
    """/entregas/painel?embed=1 troca o DENY por SAMEORIGIN pra poder ser
    embutido na tela de testes (same-origin)."""
    c = _staff(app)
    r = c.get('/entregas/painel?embed=1')
    assert r.status_code == 200
    assert r.headers.get('X-Frame-Options') == 'SAMEORIGIN'
    assert "frame-ancestors 'self'" in r.headers.get('Content-Security-Policy', '')


def test_painel_producao_sem_embed_continua_deny(app):
    """REGRESSAO: o painel de producao (sem ?embed) NAO pode ser afrouxado —
    segue X-Frame-Options DENY (anti-clickjacking)."""
    c = _staff(app)
    r = c.get('/entregas/painel')
    assert r.status_code == 200
    assert r.headers.get('X-Frame-Options') == 'DENY'


def test_painel_testes_exige_login(app):
    c = app.test_client()
    r = c.get('/entregas/painel-testes')
    assert r.status_code in (301, 302)
    assert '/login' in r.headers.get('Location', '')


# ── /entregas/api/painel-testes/chatwoot-pending ───────────


def test_pending_api_retorna_ids(app, monkeypatch):
    """Devolve IDs de conversas pending (formato esperado pelo front)."""
    from app.services import chatwoot
    monkeypatch.setattr(
        chatwoot, 'listar_conversas_paradas',
        lambda **kw: [
            {'id': 198, 'nome_contato': 'Caio', 'minutos_paradas': 0},
            {'id': 201, 'nome_contato': 'Ana', 'minutos_paradas': 2},
        ])
    c = _staff(app)
    r = c.get('/entregas/api/painel-testes/chatwoot-pending')
    assert r.status_code == 200
    j = r.get_json()
    assert j['ids'] == [198, 201]
    assert j['count'] == 2


def test_pending_api_chama_com_min_minutos_zero(app, monkeypatch):
    """REGRESSAO: precisa pegar conversas RECEM-CRIADAS (0min), nao paradas
    ha 15min. Se voltar pro default da funcao do vigia, perde alarme de
    conversa nova — proposito DESTA tela."""
    chamadas = []
    from app.services import chatwoot
    monkeypatch.setattr(
        chatwoot, 'listar_conversas_paradas',
        lambda **kw: (chamadas.append(kw) or []))
    c = _staff(app)
    c.get('/entregas/api/painel-testes/chatwoot-pending')
    assert chamadas
    assert chamadas[0].get('min_minutos') == 0
    assert chamadas[0].get('status') == 'pending'


def test_pending_api_erro_no_chatwoot_devolve_zero(app, monkeypatch):
    """Chatwoot fora do ar / token invalido NAO derruba a rota — devolve
    count=0 (frontend trata como 'nenhuma pending', nao falsifica klaxon)."""
    from app.services import chatwoot

    def boom(**kw):
        raise RuntimeError('chatwoot down')

    monkeypatch.setattr(chatwoot, 'listar_conversas_paradas', boom)
    c = _staff(app)
    r = c.get('/entregas/api/painel-testes/chatwoot-pending')
    assert r.status_code == 200
    assert r.get_json() == {'ids': [], 'count': 0}


def test_pending_api_exige_login(app):
    c = app.test_client()
    r = c.get('/entregas/api/painel-testes/chatwoot-pending')
    assert r.status_code in (301, 302)


def test_painel_testes_tem_botao_ligar_alertas(app):
    """UI: botao de armar audio (AudioContext exige user gesture) +
    chamada da API de pending no JS."""
    app.config['CHATWOOT_URL'] = 'https://atendimento.exemplo.com'
    c = _staff(app)
    r = c.get('/entregas/painel-testes')
    html = r.data.decode()
    assert 'LIGAR ALERTAS' in html
    assert '/entregas/api/painel-testes/chatwoot-pending' in html
    assert 'klaxon' in html

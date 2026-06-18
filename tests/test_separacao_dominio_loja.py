"""Separação de domínio: opao.online = só a LOJA; gestao.* = sistema full.

- Em hosts de loja (LOJA_HOSTS): raiz → /loja/, admin vira 404, loja/assets ok.
- Em gestao.* (host de teste padrão): nada muda.
- E-mails do CLIENTE linkam pra LOJA_BASE_URL; e-mail de staff usa APP_BASE_URL.
"""
from decimal import Decimal
from unittest.mock import patch

# ── Roteamento por host ─────────────────────────────────────────────────

def test_raiz_no_host_loja_redireciona_pra_loja(app):
    c = app.test_client()
    r = c.get('/', base_url='http://opao.online', follow_redirects=False)
    assert r.status_code == 302
    assert r.headers['Location'].endswith('/loja/')


def test_www_tambem_e_host_loja(app):
    c = app.test_client()
    r = c.get('/', base_url='http://www.opao.online', follow_redirects=False)
    assert r.status_code == 302
    assert r.headers['Location'].endswith('/loja/')


def test_admin_vira_404_no_host_loja(app):
    """Cliente em opao.online NÃO encontra a gestão: login, /admin, /pedidos,
    /rh etc. dão 404 (não 200, não redirect pro login)."""
    c = app.test_client()
    for path in ('/auth/login', '/pedidos', '/admin/loja-online',
                 '/rh', '/entregas', '/relatorios'):
        r = c.get(path, base_url='http://opao.online', follow_redirects=False)
        assert r.status_code == 404, f'{path} deveria ser 404 no host da loja'


def test_loja_responde_no_host_loja(app, monkeypatch):
    monkeypatch.setenv('LOJA_VISIVEL', '1')   # vitrine pública
    c = app.test_client()
    r = c.get('/loja/', base_url='http://opao.online')
    assert r.status_code == 200


def test_static_liberado_no_host_loja(app):
    c = app.test_client()
    r = c.get('/static/loja/loja.css', base_url='http://opao.online')
    assert r.status_code in (200, 304)


def test_health_liberado_no_host_loja(app):
    c = app.test_client()
    r = c.get('/health', base_url='http://opao.online')
    assert r.status_code == 200


def test_gestao_continua_full(app):
    """No host de gestão (host de teste padrão = localhost, fora de
    LOJA_HOSTS): admin acessível e raiz NÃO vai pra /loja."""
    c = app.test_client()
    # Login do admin responde normalmente
    r = c.get('/auth/login', follow_redirects=False)
    assert r.status_code == 200
    # Raiz não redireciona pra loja
    r2 = c.get('/', follow_redirects=False)
    loc = r2.headers.get('Location', '')
    assert not (r2.status_code == 302 and loc.endswith('/loja/'))


def test_host_loja_configuravel(app, monkeypatch):
    """LOJA_HOSTS define quais hosts são 'só loja'. Host fora da lista =
    comportamento full (não bloqueia admin)."""
    # Host aleatório não listado → admin acessível
    c = app.test_client()
    r = c.get('/auth/login', base_url='http://qualquer-outro.com',
              follow_redirects=False)
    assert r.status_code == 200


# ── Separação de URL nos e-mails ────────────────────────────────────────

class _FakePedido:
    codigo = 'ABC123'
    email_cliente = 'cliente@x.com'
    subtotal = Decimal('10')
    frete_valor = Decimal('5')
    valor_total = Decimal('15')
    modo_entrega = 'retirada'
    loja_retirada = None
    endereco_entrega = None
    data_entrega = None
    janela_entrega = None
    nf_emitida_em = None
    itens = []


def test_email_cliente_usa_loja_base_url(app):
    """Link no e-mail do cliente aponta pra LOJA_BASE_URL (opao.online),
    NÃO pro gestao."""
    from app.services import email as email_svc
    with app.app_context():
        app.config['LOJA_BASE_URL'] = 'https://opao.online'
        app.config['APP_BASE_URL'] = 'https://gestao.exemplo.com'
        app.config['POSTMARK_SERVER_TOKEN'] = 'tok'
        with patch('app.services.email.enviar',
                   return_value={'ok': True}) as ev:
            email_svc.enviar_pedido_recebido(_FakePedido())
        html = ev.call_args[0][2]
        assert 'opao.online' in html
        assert 'gestao.exemplo.com' not in html


def test_email_reset_senha_usa_loja_base_url(app):
    from app.services import email as email_svc
    with app.app_context():
        app.config['LOJA_BASE_URL'] = 'https://opao.online'
        app.config['APP_BASE_URL'] = 'https://gestao.exemplo.com'
        app.config['POSTMARK_SERVER_TOKEN'] = 'tok'

        class C:
            nome = 'Maria'
            email = 'm@x.com'
        with patch('app.services.email.enviar',
                   return_value={'ok': True}) as ev:
            email_svc.enviar_reset_senha(C(), 'tok-xyz')
        html = ev.call_args[0][2]
        assert 'opao.online/loja/redefinir-senha/tok-xyz' in html


def test_email_boas_vindas_staff_usa_app_base_url(app):
    """E-mail de onboarding de STAFF continua linkando pro admin (gestao),
    não pra loja."""
    from app.services import email as email_svc
    with app.app_context():
        app.config['LOJA_BASE_URL'] = 'https://opao.online'
        app.config['APP_BASE_URL'] = 'https://gestao.exemplo.com'
        app.config['POSTMARK_SERVER_TOKEN'] = 'tok'
        with patch('app.services.email.enviar',
                   return_value={'ok': True}) as ev:
            email_svc.enviar_boas_vindas('s@x.com', 'Staff', 'staff',
                                          'senha1234')
        html = ev.call_args[0][2]
        assert 'gestao.exemplo.com/auth/login' in html

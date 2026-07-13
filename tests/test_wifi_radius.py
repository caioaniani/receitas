"""Login do Wi-Fi via RADIUS (13/07/2026).

Cobre as DUAS peças: o endpoint /api/wifi/radius-check (valida e-mail+senha
do Cliente, anti-enumeração, guarda de token) e a cripto da ponte RADIUS
standalone (wifi_radius/bridge.py — round-trip PAP + estrutura da resposta).
"""
import importlib.util
import pathlib

from app.extensions import db

# Carrega a ponte standalone (fora do pacote app) por caminho — o guard
# __main__ impede que servir() rode no import.
_spec = importlib.util.spec_from_file_location(
    'wifi_bridge',
    pathlib.Path(__file__).resolve().parent.parent / 'wifi_radius' /
    'bridge.py')
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


def _cliente(email='joao@example.com', senha='segredo1', ativo=True):
    from app.models import Cliente
    c = Cliente(nome='João Cliente', email=email, ativo=ativo)
    if senha:
        c.set_senha(senha)
    db.session.add(c)
    db.session.commit()
    return c


def _post(app, body, token='tok-teste'):
    c = app.test_client()
    return c.post('/api/wifi/radius-check', json=body,
                  headers={'Authorization': f'Bearer {token}'})


# ── Endpoint /api/wifi/radius-check ──────────────────────────────────────

def test_sem_token_configurado_503(app):
    with app.app_context():
        app.config['WIFI_RADIUS_TOKEN'] = ''
    r = _post(app, {'email': 'x@x.com', 'senha': 'y'})
    assert r.status_code == 503


def test_token_errado_401(app):
    with app.app_context():
        app.config['WIFI_RADIUS_TOKEN'] = 'tok-teste'
        _cliente()
    r = _post(app, {'email': 'joao@example.com', 'senha': 'segredo1'},
              token='errado')
    assert r.status_code == 401


def test_credencial_certa_aceita(app):
    with app.app_context():
        app.config['WIFI_RADIUS_TOKEN'] = 'tok-teste'
        _cliente()
    r = _post(app, {'email': 'joao@example.com', 'senha': 'segredo1'})
    assert r.status_code == 200 and r.get_json()['ok'] is True


def test_senha_errada_rejeita(app):
    with app.app_context():
        app.config['WIFI_RADIUS_TOKEN'] = 'tok-teste'
        _cliente()
    r = _post(app, {'email': 'joao@example.com', 'senha': 'ERRADA'})
    assert r.status_code == 200 and r.get_json()['ok'] is False


def test_email_maiusculo_normaliza(app):
    with app.app_context():
        app.config['WIFI_RADIUS_TOKEN'] = 'tok-teste'
        _cliente()
    r = _post(app, {'email': 'JOAO@Example.com', 'senha': 'segredo1'})
    assert r.get_json()['ok'] is True


def test_conta_inexistente_rejeita_sem_vazar(app):
    with app.app_context():
        app.config['WIFI_RADIUS_TOKEN'] = 'tok-teste'
    r = _post(app, {'email': 'ninguem@example.com', 'senha': 'x'})
    # mesma resposta que senha errada — anti-enumeração
    assert r.status_code == 200 and r.get_json()['ok'] is False


def test_guest_sem_senha_rejeita(app):
    with app.app_context():
        app.config['WIFI_RADIUS_TOKEN'] = 'tok-teste'
        _cliente(email='guest@example.com', senha=None)   # senha_hash NULL
    r = _post(app, {'email': 'guest@example.com', 'senha': 'qualquer'})
    assert r.get_json()['ok'] is False


def test_cliente_inativo_rejeita(app):
    with app.app_context():
        app.config['WIFI_RADIUS_TOKEN'] = 'tok-teste'
        _cliente(email='off@example.com', ativo=False)
    r = _post(app, {'email': 'off@example.com', 'senha': 'segredo1'})
    assert r.get_json()['ok'] is False


def test_faltou_campo_rejeita(app):
    with app.app_context():
        app.config['WIFI_RADIUS_TOKEN'] = 'tok-teste'
    r = _post(app, {'email': 'joao@example.com'})
    assert r.status_code == 200 and r.get_json()['ok'] is False


def test_ping_com_token(app):
    with app.app_context():
        app.config['WIFI_RADIUS_TOKEN'] = 'tok-teste'
    c = app.test_client()
    r = c.get('/api/wifi/ping',
              headers={'Authorization': 'Bearer tok-teste'})
    assert r.status_code == 200 and r.get_json()['ok'] is True


# ── Cripto da ponte RADIUS ───────────────────────────────────────────────

def test_pap_round_trip():
    secret = b'segredo-radius'
    req_auth = bytes(range(16))
    for senha in ('', 'a', 'segredo1', 'x' * 16, 'áçã🥐'):
        cif = bridge.encrypt_pap(senha, secret, req_auth)
        assert len(cif) % 16 == 0
        assert bridge.decrypt_pap(cif, secret, req_auth) == senha


def test_build_reply_estrutura():
    import struct
    secret = b's'
    req_auth = bytes(16)
    reply = bridge.build_reply(bridge.CODE_ACCESS_ACCEPT, 42, req_auth, secret)
    assert reply[0] == bridge.CODE_ACCESS_ACCEPT
    assert reply[1] == 42
    assert struct.unpack('!H', reply[2:4])[0] == len(reply)
    # tem o Message-Authenticator (tipo 80, 18 bytes)
    attrs = bridge.parse_attributes(reply[20:])
    assert bridge.ATTR_MESSAGE_AUTHENTICATOR in attrs


def test_tratar_pacote_nao_request_ignora():
    # um Access-Accept (code 2) não é request → None
    import struct
    pkt = struct.pack('!BBH', 2, 1, 20) + bytes(16)
    assert bridge.tratar_pacote(pkt, b'x') is None


def test_tratar_pacote_valida_e_responde(monkeypatch):
    import struct
    secret = b'segredo'
    req_auth = bytes(range(16))
    user = b'joao@example.com'
    senha_cif = bridge.encrypt_pap('segredo1', secret, req_auth)
    attrs = (bytes([bridge.ATTR_USER_NAME, len(user) + 2]) + user
             + bytes([bridge.ATTR_USER_PASSWORD, len(senha_cif) + 2])
             + senha_cif)
    length = 20 + len(attrs)
    pkt = struct.pack('!BBH', 1, 9, length) + req_auth + attrs
    capturado = {}

    def fake_validar(email, senha):
        capturado['email'] = email
        capturado['senha'] = senha
        return True
    monkeypatch.setattr(bridge, 'validar_no_gestao', fake_validar)
    resp = bridge.tratar_pacote(pkt, secret)
    assert capturado == {'email': 'joao@example.com', 'senha': 'segredo1'}
    assert resp[0] == bridge.CODE_ACCESS_ACCEPT

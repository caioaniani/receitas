#!/usr/bin/env python3
"""Ponte RADIUS do Wi-Fi das lojas (13/07/2026) — O Pão Padaria Artesanal.

RODA NUM SERVIDORZINHO (VPS), **não** no Railway/gestão.opao — o Railway não
expõe UDP, e o RADIUS é UDP. Esta ponte é só um TRADUTOR de protocolo:

    OC200 (portal RADIUS)  --UDP 1812-->  [esta ponte]  --HTTPS-->  gestão.opao
                                                                    /api/wifi/radius-check

Fluxo: o cliente digita e-mail+senha na tela do Omada → o OC200 manda um
Access-Request (com a senha cifrada por PAP) → a ponte decripta a senha,
pergunta ao gestão.opao "confere?" → devolve Access-Accept ou Access-Reject.

Sem dependências além da biblioteca padrão do Python 3.8+ (urllib, hashlib,
hmac, socket). Deploy: ver README.md nesta pasta.

Config por variáveis de ambiente:
    WIFI_RADIUS_SECRET   segredo RADIUS compartilhado com o OC200 (obrigatório)
    WIFI_API_URL         ex: https://gestao.opaopadariaartesanal.com.br/api/wifi
    WIFI_API_TOKEN       = WIFI_RADIUS_TOKEN do gestão.opao (Bearer)
    WIFI_RADIUS_HOST     bind (default 0.0.0.0)
    WIFI_RADIUS_PORT     bind (default 1812)
    WIFI_API_TIMEOUT     timeout HTTP em segundos (default 8)

Teste local do miolo cripto (sem subir servidor):  python3 bridge.py --selftest
"""
import hashlib
import hmac
import json
import logging
import os
import socket
import struct
import sys
import urllib.error
import urllib.request

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s wifi-radius %(levelname)s %(message)s')
log = logging.getLogger('wifi-radius')

CODE_ACCESS_REQUEST = 1
CODE_ACCESS_ACCEPT = 2
CODE_ACCESS_REJECT = 3

ATTR_USER_NAME = 1
ATTR_USER_PASSWORD = 2
ATTR_MESSAGE_AUTHENTICATOR = 80


# ── RADIUS: parse + cripto (RFC 2865 / 3579) ─────────────────────────────

def parse_attributes(data):
    """data = bytes das attributes. Retorna dict {tipo: [valor, ...]}."""
    attrs = {}
    i = 0
    while i + 2 <= len(data):
        tipo = data[i]
        comprimento = data[i + 1]
        if comprimento < 2 or i + comprimento > len(data):
            break
        valor = data[i + 2:i + comprimento]
        attrs.setdefault(tipo, []).append(valor)
        i += comprimento
    return attrs


def decrypt_pap(cipher, secret, req_auth):
    """Decripta o User-Password (PAP, RFC 2865 §5.2). Retorna a senha em
    texto (sem o padding de nulos)."""
    if not cipher or len(cipher) % 16 != 0:
        return ''
    out = bytearray()
    anterior = req_auth
    for off in range(0, len(cipher), 16):
        bloco = cipher[off:off + 16]
        b = hashlib.md5(secret + anterior).digest()
        out.extend(x ^ y for x, y in zip(bloco, b))
        anterior = bloco
    return out.rstrip(b'\x00').decode('utf-8', errors='replace')


def encrypt_pap(plain, secret, req_auth):
    """Cifra a senha (PAP) — usado só no --selftest (o OC200 é quem cifra
    em produção)."""
    dados = plain.encode('utf-8')
    if len(dados) % 16 != 0:
        dados += b'\x00' * (16 - len(dados) % 16)
    out = bytearray()
    anterior = req_auth
    for off in range(0, len(dados), 16):
        bloco = dados[off:off + 16]
        b = hashlib.md5(secret + anterior).digest()
        cifrado = bytes(x ^ y for x, y in zip(bloco, b))
        out.extend(cifrado)
        anterior = cifrado
    return bytes(out)


def _encode_attr(tipo, valor):
    return bytes([tipo, len(valor) + 2]) + valor


def build_reply(code, ident, req_auth, secret):
    """Monta um Access-Accept/Reject com Message-Authenticator (RFC 3579)
    + Response Authenticator (RFC 2865). Ordem importa:
      1. attrs com Message-Authenticator = 16 zeros;
      2. campo Authenticator = Request Authenticator; calcula o MA (HMAC-MD5);
      3. preenche o MA; calcula o Response Authenticator; troca o campo."""
    ma_placeholder = _encode_attr(ATTR_MESSAGE_AUTHENTICATOR, b'\x00' * 16)
    length = 20 + len(ma_placeholder)
    cabecalho = struct.pack('!BBH', code, ident, length)

    # (2) MA = HMAC-MD5(secret) sobre o pacote com Authenticator=req_auth e
    # o campo MA zerado.
    base = cabecalho + req_auth + ma_placeholder
    ma = hmac.new(secret, base, hashlib.md5).digest()
    attrs = _encode_attr(ATTR_MESSAGE_AUTHENTICATOR, ma)

    # (3) Response Authenticator = MD5(Code+ID+Length+RequestAuth+Attrs+Secret)
    resp_auth = hashlib.md5(
        cabecalho + req_auth + attrs + secret).digest()
    return cabecalho + resp_auth + attrs


# ── Validação contra o gestão.opao ───────────────────────────────────────

def validar_no_gestao(email, senha):
    """Pergunta ao endpoint /api/wifi/radius-check. Retorna True/False.
    Em QUALQUER erro (rede, timeout, 5xx) devolve False — fail-closed: se a
    ponte não consegue confirmar, NÃO libera (segurança > conveniência)."""
    url = os.environ.get('WIFI_API_URL', '').rstrip('/') + '/radius-check'
    token = os.environ.get('WIFI_API_TOKEN', '')
    timeout = float(os.environ.get('WIFI_API_TIMEOUT', '8'))
    corpo = json.dumps({'email': email, 'senha': senha}).encode('utf-8')
    req = urllib.request.Request(url, data=corpo, method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('Authorization', f'Bearer {token}')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return bool(data.get('ok'))
    except urllib.error.HTTPError as e:
        log.warning('gestão respondeu HTTP %s pra %s', e.code, email)
        return False
    except Exception as e:  # noqa: BLE001 — fail-closed em qualquer falha
        log.warning('falha ao validar %s: %s', email, e)
        return False


# ── Servidor UDP ─────────────────────────────────────────────────────────

def tratar_pacote(data, secret):
    """Recebe os bytes de um Access-Request, devolve os bytes da resposta
    (ou None se o pacote não for um Access-Request válido)."""
    if len(data) < 20:
        return None
    code, ident, length = struct.unpack('!BBH', data[:4])
    if code != CODE_ACCESS_REQUEST:
        return None
    req_auth = data[4:20]
    attrs = parse_attributes(data[20:length] if length <= len(data)
                             else data[20:])
    user = attrs.get(ATTR_USER_NAME, [b''])[0].decode(
        'utf-8', errors='replace').strip()
    senha_cif = attrs.get(ATTR_USER_PASSWORD, [b''])[0]
    if not user or not senha_cif:
        log.info('Access-Request sem user/senha — reject')
        return build_reply(CODE_ACCESS_REJECT, ident, req_auth, secret)
    senha = decrypt_pap(senha_cif, secret, req_auth)
    ok = validar_no_gestao(user.lower(), senha)
    log.info('%s -> %s', user, 'ACCEPT' if ok else 'REJECT')
    code_resp = CODE_ACCESS_ACCEPT if ok else CODE_ACCESS_REJECT
    return build_reply(code_resp, ident, req_auth, secret)


def servir():
    secret = os.environ.get('WIFI_RADIUS_SECRET', '').encode('utf-8')
    if not secret:
        log.error('WIFI_RADIUS_SECRET nao definido — abortando')
        sys.exit(1)
    if not os.environ.get('WIFI_API_URL') or not os.environ.get(
            'WIFI_API_TOKEN'):
        log.error('WIFI_API_URL/WIFI_API_TOKEN nao definidos — abortando')
        sys.exit(1)
    host = os.environ.get('WIFI_RADIUS_HOST', '0.0.0.0')
    port = int(os.environ.get('WIFI_RADIUS_PORT', '1812'))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    log.info('ponte RADIUS ouvindo em %s:%s', host, port)
    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except OSError as e:
            log.warning('recvfrom falhou: %s', e)
            continue
        try:
            resp = tratar_pacote(data, secret)
            if resp:
                sock.sendto(resp, addr)
        except Exception:  # noqa: BLE001 — um pacote ruim nunca derruba a ponte
            log.exception('erro tratando pacote de %s', addr)


def selftest():
    """Round-trip da cripto PAP + sanidade do build_reply (sem rede)."""
    secret = b'segredo-de-teste'
    req_auth = bytes(range(16))
    for senha in ('', 'a', 'segredo1', 'senha-de-16-bytes!!', 'áçãinfo🥐'):
        cif = encrypt_pap(senha, secret, req_auth)
        assert decrypt_pap(cif, secret, req_auth) == senha, senha
    reply = build_reply(CODE_ACCESS_ACCEPT, 7, req_auth, secret)
    assert reply[0] == CODE_ACCESS_ACCEPT and reply[1] == 7
    assert struct.unpack('!H', reply[2:4])[0] == len(reply)
    print('selftest OK — cripto PAP e build_reply corretos')


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        selftest()
    else:
        servir()

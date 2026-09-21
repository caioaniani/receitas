"""Proteção local dos dados Fiserv, sem arquivos temporários, rede ou banco.

A SECRET_KEY estável do servidor é a raiz das duas chaves derivadas. Sua troca
invalida tokens anteriores; não há fallback para chave guardada no banco.
PPK: manual oficial PuTTY, apêndice C (formato 3, RSA, Encryption: none).
https://the.earth.li/~sgtatham/putty/0.85/htmldoc/AppendixC.html
"""

import base64
import hashlib
import hmac
import io
import json
import math
import re
from functools import wraps

import paramiko
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from flask import current_app

MAX_JSON_BYTES = 64 * 1024
MAX_CHAVE_BYTES = 256 * 1024
MAX_EDI_BYTES = 25 * 1024 * 1024
MAX_PASSPHRASE_BYTES = 1024
MAX_OPENSSH_BCRYPT_ROUNDS = 64
_CONTEXTO_SEGREDOS = b'fiserv-segredos-v1'
_CONTEXTO_EDI = b'fiserv-edi-bruto-v1'
_MAX_NOS_JSON = 8192
_MAX_PROFUNDIDADE_JSON = 16
_MAX_LINHAS_PPK = 4096
_MAX_MPINT_BYTES = 1025  # RSA de até 8192 bits, incluindo o byte de sinal SSH.
_MENSAGENS = {
    'configuracao': 'A chave de proteção do servidor não está configurada corretamente.',
    'dados': 'Os dados privados são inválidos ou excedem o limite permitido.',
    'token': 'Não foi possível abrir os dados protegidos. Confira a configuração do servidor.',
    'edi': 'O arquivo bancário é inválido ou excede o limite permitido.',
    'chave': 'A chave privada é inválida, está danificada ou sua frase de proteção está incorreta.',
    'ppk_formato': 'Formato PPK não homologado. Use PPK v3 RSA sem criptografia, PEM ou OpenSSH.',
    'ppk_cifrado': 'PPK criptografado ainda não é suportado. '
                  'Use PEM sem criptografia ou OpenSSH com bcrypt de até 64 rodadas.',
    'pem_cifrado': 'PEM criptografado não é suportado porque seu custo de derivação não pode ser limitado. '
                  'Use PEM sem criptografia ou OpenSSH com bcrypt de até 64 rodadas.',
    'openssh_custo': 'A proteção da chave OpenSSH não é suportada ou excede o limite de 64 rodadas bcrypt.',
}


class ErroSegredoFiserv(ValueError):
    """Erro público cujo texto e argumentos nunca contêm dados recebidos."""

    def __init__(self, codigo='dados'):
        __tracebackhide__ = True
        self.codigo = codigo if isinstance(codigo, str) and codigo in _MENSAGENS else 'dados'
        super().__init__(_MENSAGENS[self.codigo])


def _sanitizar(codigo_padrao):
    __tracebackhide__ = True

    def decorar(funcao):
        __tracebackhide__ = True

        @wraps(funcao)
        def protegido(*args, **kwargs):
            __tracebackhide__ = True
            try:
                return funcao(*args, **kwargs)
            except ErroSegredoFiserv as exc:
                codigo = exc.codigo
            except Exception:
                # Bibliotecas podem incluir bytes/frases em args ou frames.
                # Descartar a exceção completa, inclusive causa e contexto.
                codigo = codigo_padrao
            args = ()
            kwargs = {}
            # Fora do except: nem __context__ conserva a exceção privada.
            raise ErroSegredoFiserv(codigo) from None

        return protegido

    return decorar


def _fernet(contexto):
    __tracebackhide__ = True
    try:
        segredo = current_app.config.get('SECRET_KEY')
        if isinstance(segredo, str):
            segredo = segredo.encode('utf-8')
        if not isinstance(segredo, bytes) or not segredo or len(segredo) > 4096:
            raise ValueError
        chave = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=contexto).derive(segredo)
        return Fernet(base64.urlsafe_b64encode(chave))
    except Exception:
        pass
    raise ErroSegredoFiserv('configuracao') from None


def _validar_json(dados):
    __tracebackhide__ = True
    if type(dados) is not dict:
        raise ErroSegredoFiserv('dados')
    pendentes = [(dados, 0)]
    quantidade = 0
    texto_bytes = 0
    while pendentes:
        valor, nivel = pendentes.pop()
        quantidade += 1
        if quantidade > _MAX_NOS_JSON or nivel > _MAX_PROFUNDIDADE_JSON:
            raise ErroSegredoFiserv('dados')
        if type(valor) is dict:
            if len(valor) + len(pendentes) > _MAX_NOS_JSON - quantidade:
                raise ErroSegredoFiserv('dados')
            for chave, item in valor.items():
                if type(chave) is not str or len(chave) > MAX_JSON_BYTES:
                    raise ErroSegredoFiserv('dados')
                texto_bytes += len(chave.encode('utf-8'))
                pendentes.append((item, nivel + 1))
        elif type(valor) is list:
            if len(valor) + len(pendentes) > _MAX_NOS_JSON - quantidade:
                raise ErroSegredoFiserv('dados')
            for item in valor:
                pendentes.append((item, nivel + 1))
        elif type(valor) is str:
            if len(valor) > MAX_JSON_BYTES:
                raise ErroSegredoFiserv('dados')
            texto_bytes += len(valor.encode('utf-8'))
        elif type(valor) is float:
            if not math.isfinite(valor):
                raise ErroSegredoFiserv('dados')
        elif type(valor) is int:
            if valor.bit_length() > 256:
                raise ErroSegredoFiserv('dados')
        elif valor is not None and type(valor) is not bool:
            raise ErroSegredoFiserv('dados')
        if texto_bytes > MAX_JSON_BYTES:
            raise ErroSegredoFiserv('dados')


def _abrir_token(token, contexto, limite):
    __tracebackhide__ = True
    # Fernet = versão + tempo + IV + PKCS7 + HMAC, codificado em base64.
    limite_token = 4 * ((57 + 16 * (limite // 16 + 1) + 2) // 3)
    if type(token) is not str or not token or len(token) > limite_token:
        raise ErroSegredoFiserv('token')
    bruto = _fernet(contexto).decrypt(token.encode('ascii'))
    if len(bruto) > limite:
        raise ErroSegredoFiserv('token')
    return bruto


@_sanitizar('dados')
def cifrar_json(dados: dict) -> str:
    __tracebackhide__ = True
    _validar_json(dados)
    bruto = json.dumps(dados, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(bruto) > MAX_JSON_BYTES:
        raise ErroSegredoFiserv('dados')
    return _fernet(_CONTEXTO_SEGREDOS).encrypt(bruto).decode('ascii')


@_sanitizar('token')
def decifrar_json(token: str) -> dict:
    __tracebackhide__ = True
    bruto = _abrir_token(token, _CONTEXTO_SEGREDOS, MAX_JSON_BYTES)
    dados = json.loads(bruto)
    _validar_json(dados)
    return dados


@_sanitizar('edi')
def cifrar_bytes(conteudo: bytes) -> str:
    __tracebackhide__ = True
    if type(conteudo) is not bytes or len(conteudo) > MAX_EDI_BYTES:
        raise ErroSegredoFiserv('edi')
    return _fernet(_CONTEXTO_EDI).encrypt(conteudo).decode('ascii')


@_sanitizar('token')
def decifrar_bytes(token: str) -> bytes:
    __tracebackhide__ = True
    return _abrir_token(token, _CONTEXTO_EDI, MAX_EDI_BYTES)


def _campo_ppk(linhas, indice, nome):
    __tracebackhide__ = True
    prefixo = nome + b': '
    if indice >= len(linhas) or not linhas[indice].startswith(prefixo):
        raise ErroSegredoFiserv('chave')
    return linhas[indice][len(prefixo):]


def _blob_ppk(linhas, indice, nome):
    __tracebackhide__ = True
    contagem = _campo_ppk(linhas, indice, nome)
    if not re.fullmatch(rb'[0-9]{1,4}', contagem):
        raise ErroSegredoFiserv('chave')
    quantidade = int(contagem)
    inicio = indice + 1
    fim = inicio + quantidade
    if not 1 <= quantidade <= _MAX_LINHAS_PPK or fim > len(linhas):
        raise ErroSegredoFiserv('chave')
    for linha in linhas[inicio:fim]:
        if not linha or len(linha) > 1024:
            raise ErroSegredoFiserv('chave')
    return base64.b64decode(b''.join(linhas[inicio:fim]), validate=True), fim


def _ssh_string(valor):
    __tracebackhide__ = True
    return len(valor).to_bytes(4, 'big') + valor


def _ler_string(blob, indice):
    __tracebackhide__ = True
    if indice + 4 > len(blob):
        raise ErroSegredoFiserv('chave')
    tamanho = int.from_bytes(blob[indice:indice + 4], 'big')
    inicio = indice + 4
    fim = inicio + tamanho
    if tamanho > _MAX_MPINT_BYTES or fim > len(blob):
        raise ErroSegredoFiserv('chave')
    return blob[inicio:fim], fim


def _ler_mpint(blob, indice):
    __tracebackhide__ = True
    valor, fim = _ler_string(blob, indice)
    if not valor or valor[0] & 0x80:
        raise ErroSegredoFiserv('chave')
    if valor[0] == 0 and (len(valor) == 1 or not valor[1] & 0x80):
        raise ErroSegredoFiserv('chave')
    return int.from_bytes(valor, 'big'), fim


def _carregar_ppk(conteudo):
    __tracebackhide__ = True
    # splitlines também separaria bytes permitidos no comentário (VT/FF).
    linhas = conteudo.replace(b'\r\n', b'\n').replace(b'\r', b'\n').split(b'\n')
    while linhas and linhas[-1] == b'':
        linhas.pop()
    if len(linhas) > _MAX_LINHAS_PPK or not linhas:
        raise ErroSegredoFiserv('chave')
    if linhas[0] != b'PuTTY-User-Key-File-3: ssh-rsa':
        raise ErroSegredoFiserv('ppk_formato')
    algoritmo = b'ssh-rsa'
    criptografia = _campo_ppk(linhas, 1, b'Encryption')
    if criptografia != b'none':
        raise ErroSegredoFiserv('ppk_cifrado')
    comentario = _campo_ppk(linhas, 2, b'Comment')
    publico, indice = _blob_ppk(linhas, 3, b'Public-Lines')
    privado, indice = _blob_ppk(linhas, indice, b'Private-Lines')
    mac = _campo_ppk(linhas, indice, b'Private-MAC')
    if indice != len(linhas) - 1 or not re.fullmatch(rb'[0-9a-fA-F]{64}', mac):
        raise ErroSegredoFiserv('chave')
    calculo = hmac.new(b'', digestmod=hashlib.sha256)
    for parte in (algoritmo, criptografia, comentario, publico, privado):
        calculo.update(_ssh_string(parte))
    if not hmac.compare_digest(calculo.hexdigest().encode('ascii'), mac.lower()):
        raise ErroSegredoFiserv('chave')

    tipo, posicao = _ler_string(publico, 0)
    if tipo != algoritmo:
        raise ErroSegredoFiserv('chave')
    e, posicao = _ler_mpint(publico, posicao)
    n, posicao = _ler_mpint(publico, posicao)
    if posicao != len(publico) or not 1024 <= n.bit_length() <= 8192:
        raise ErroSegredoFiserv('chave')
    d, posicao = _ler_mpint(privado, 0)
    p, posicao = _ler_mpint(privado, posicao)
    q, posicao = _ler_mpint(privado, posicao)
    iqmp, posicao = _ler_mpint(privado, posicao)
    # O formato permite padding final; seu conteúdo já foi coberto pelo MAC.
    if len(privado) - posicao > 15 or p <= 2 or q <= 2:
        raise ErroSegredoFiserv('chave')
    numeros = rsa.RSAPrivateNumbers(
        p=p, q=q, d=d, dmp1=d % (p - 1), dmq1=d % (q - 1), iqmp=iqmp,
        public_numbers=rsa.RSAPublicNumbers(e=e, n=n),
    )
    # Não usar unsafe_skip_rsa_key_validation: MAC sem senha não substitui
    # validação matemática dos primos, expoentes e correspondência pública.
    return paramiko.RSAKey(key=numeros.private_key())


def _para_paramiko(chave):
    __tracebackhide__ = True
    if isinstance(chave, rsa.RSAPrivateKey):
        return paramiko.RSAKey(key=chave)
    if isinstance(chave, ec.EllipticCurvePrivateKey):
        if not isinstance(chave.curve, (ec.SECP256R1, ec.SECP384R1, ec.SECP521R1)):
            raise ErroSegredoFiserv('chave')
        return paramiko.ECDSAKey(vals=(chave, chave.public_key()))
    if isinstance(chave, ed25519.Ed25519PrivateKey):
        serializada = chave.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption(),
        )
        with io.StringIO(serializada.decode('ascii')) as arquivo_memoria:
            return paramiko.Ed25519Key.from_private_key(arquivo_memoria)
    raise ErroSegredoFiserv('chave')


def _decodificar_armadura(conteudo, rotulo):
    __tracebackhide__ = True
    linhas = conteudo.strip().splitlines()
    if (len(linhas) < 3 or linhas[0] != b'-----BEGIN ' + rotulo + b'-----'
            or linhas[-1] != b'-----END ' + rotulo + b'-----'):
        raise ErroSegredoFiserv('chave')
    # Uma única armadura estrita: a biblioteca não pode escolher outro bloco
    # diferente daquele cujos parâmetros foram inspecionados aqui.
    return base64.b64decode(b''.join(linhas[1:-1]), validate=True)


def _validar_custo_openssh(conteudo):
    __tracebackhide__ = True
    bruto = _decodificar_armadura(conteudo, b'OPENSSH PRIVATE KEY')
    magic = b'openssh-key-v1\x00'
    if not bruto.startswith(magic):
        raise ErroSegredoFiserv('chave')
    cifra, posicao = _ler_string(bruto, len(magic))
    kdf, posicao = _ler_string(bruto, posicao)
    parametros, posicao = _ler_string(bruto, posicao)
    if cifra == b'none' and kdf == b'none' and not parametros:
        return
    if (cifra not in (b'aes256-ctr', b'aes256-cbc', b'aes256-gcm@openssh.com')
            or kdf != b'bcrypt'):
        raise ErroSegredoFiserv('openssh_custo')
    salt, posicao = _ler_string(parametros, 0)
    if not 1 <= len(salt) <= 64 or len(parametros) - posicao != 4:
        raise ErroSegredoFiserv('openssh_custo')
    rodadas = int.from_bytes(parametros[posicao:], 'big')
    if not 1 <= rodadas <= MAX_OPENSSH_BCRYPT_ROUNDS:
        raise ErroSegredoFiserv('openssh_custo')


def _validar_pem_sem_criptografia(conteudo):
    __tracebackhide__ = True
    linhas = conteudo.strip().splitlines()
    if (linhas[0] == b'-----BEGIN ENCRYPTED PRIVATE KEY-----'
            or any(linha.startswith((b'Proc-Type:', b'DEK-Info:')) for linha in linhas[1:-1])):
        raise ErroSegredoFiserv('pem_cifrado')
    for rotulo in (b'PRIVATE KEY', b'RSA PRIVATE KEY', b'EC PRIVATE KEY'):
        if linhas[0] == b'-----BEGIN ' + rotulo + b'-----':
            der = _decodificar_armadura(conteudo, rotulo)
            tag, sequencia, fim = _ler_elemento_der(der, 0)
            if tag != 0x30 or fim != len(der):
                raise ErroSegredoFiserv('chave')
            tag, versao, fim = _ler_elemento_der(sequencia, 0)
            # PKCS8, PKCS1/RSA e SEC1/EC começam com versão INTEGER.
            # EncryptedPrivateKeyInfo começa com AlgorithmIdentifier SEQUENCE;
            # recusar também quando alguém falsifica o rótulo da armadura.
            if tag == 0x30:
                raise ErroSegredoFiserv('pem_cifrado')
            if tag != 2 or versao not in (b'\x00', b'\x01'):
                raise ErroSegredoFiserv('chave')
            return
    raise ErroSegredoFiserv('chave')


def _ler_elemento_der(bruto, posicao):
    __tracebackhide__ = True
    if len(bruto) - posicao < 2:
        raise ErroSegredoFiserv('chave')
    tag, tamanho = bruto[posicao:posicao + 2]
    inicio = posicao + 2
    if tamanho & 0x80:
        quantidade = tamanho & 0x7f
        if not 1 <= quantidade <= 4 or inicio + quantidade > len(bruto):
            raise ErroSegredoFiserv('chave')
        tamanho = int.from_bytes(bruto[inicio:inicio + quantidade], 'big')
        inicio += quantidade
    fim = inicio + tamanho
    if fim > len(bruto):
        raise ErroSegredoFiserv('chave')
    return tag, bruto[inicio:fim], fim


@_sanitizar('chave')
def carregar_chave(conteudo: bytes, passphrase: str | None = None) -> paramiko.PKey:
    """Abre chaves em memória, limitando KDF antes da biblioteca criptográfica.

    Aceita PEM sem criptografia, PPK v3 RSA sem criptografia e OpenSSH sem
    criptografia ou com bcrypt de 1 a 64 rodadas. PEM cifrado é recusado:
    seus parâmetros PBES/PBKDF não são inspecionados por este módulo.
    """
    __tracebackhide__ = True
    if type(conteudo) is not bytes or not conteudo or len(conteudo) > MAX_CHAVE_BYTES:
        raise ErroSegredoFiserv('chave')
    if passphrase is not None and type(passphrase) is not str:
        raise ErroSegredoFiserv('chave')
    senha = passphrase.encode('utf-8') if passphrase else None
    if senha is not None and len(senha) > MAX_PASSPHRASE_BYTES:
        raise ErroSegredoFiserv('chave')
    if conteudo.startswith(b'PuTTY-User-Key-File-'):
        return _carregar_ppk(conteudo)
    if conteudo.startswith(b'-----BEGIN OPENSSH PRIVATE KEY-----'):
        _validar_custo_openssh(conteudo)
        chave = serialization.load_ssh_private_key(conteudo, password=senha)
    elif conteudo.startswith((b'-----BEGIN PRIVATE KEY-----', b'-----BEGIN ENCRYPTED PRIVATE KEY-----',
                             b'-----BEGIN RSA PRIVATE KEY-----', b'-----BEGIN EC PRIVATE KEY-----')):
        _validar_pem_sem_criptografia(conteudo)
        # Nunca entregar senha ao decodificador PEM: mesmo um DER cifrado
        # disfarçado com rótulo incorreto não deve iniciar uma derivação.
        chave = serialization.load_pem_private_key(conteudo, password=None)
    else:
        raise ErroSegredoFiserv('chave')
    return _para_paramiko(chave)

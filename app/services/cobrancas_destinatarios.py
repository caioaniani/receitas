"""Validação dos endereços de cobrança, sem emitir documentos ou enviar e-mail."""
import re
from email.errors import MessageError
from email.headerregistry import Address

from flask import current_app
from itsdangerous import BadSignature, URLSafeSerializer

MAX_ADICIONAIS = 10
MAX_TEXTO = 3000


def email_valido(valor):
    if not (isinstance(valor, str) and valor and len(valor) <= 254
            and not any(ord(c) < 32 or ord(c) == 127 for c in valor)
            and re.fullmatch(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+', valor)):
        return False
    try:
        endereco = Address(addr_spec=valor)
        dominio = endereco.domain.encode('idna').decode('ascii')
    except (ValueError, MessageError, UnicodeError):
        return False
    # Aceitar apenas a forma simples exibida, sem comentários ou aliases
    # com aspas que virem outra string na API e escapem da deduplicação.
    return bool(endereco.addr_spec == valor and endereco.username and len(endereco.username) <= 64
                and re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
                                 r'(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+', dominio))


def normalizar_adicionais(valor, principal=''):
    """Aceita campo de texto ou JSON; cada endereço é validado, sem truncar."""
    if valor is None or valor == '':
        return []
    if isinstance(valor, str):
        if len(valor) > MAX_TEXTO:
            raise ValueError('A lista de e-mails adicionais é muito longa.')
        valores = re.split(r'[,;\r\n]+', valor)
    elif isinstance(valor, list) and all(isinstance(v, str) for v in valor):
        if sum(len(v) for v in valor) > MAX_TEXTO:
            raise ValueError('A lista de e-mails adicionais é muito longa.')
        valores = valor
    else:
        raise ValueError('Informe os e-mails adicionais separados por vírgula ou um por linha.')
    vistos = {(principal or '').strip().casefold()}
    emails = []
    for entrada in valores:
        endereco = entrada.strip()
        if not endereco:
            continue
        if not email_valido(endereco):
            raise ValueError(f'E-mail adicional inválido: {endereco}. Confira o cadastro do cliente.')
        if endereco.casefold() not in vistos:
            emails.append(endereco)
            vistos.add(endereco.casefold())
    if len(emails) > MAX_ADICIONAIS:
        raise ValueError(f'Informe no máximo {MAX_ADICIONAIS} e-mails adicionais para cobrança.')
    return emails


def copias_do_documento(documento, principal):
    cliente = getattr(documento, 'cliente', None)
    return normalizar_adicionais(getattr(cliente, 'emails_cobranca', None), principal)


def _estado_copias(documento):
    return {'tabela': getattr(documento, '__tablename__', None),
            'id': getattr(documento, 'id', None), 'copias': copias_do_documento(documento, '')}


def _assinador():
    return URLSafeSerializer(current_app.secret_key, salt='cobrancas-copias-v1')


def versao_copias(documento):
    return _assinador().dumps(_estado_copias(documento))


def conferir_versao_copias(documento, versao):
    atual = _estado_copias(documento)
    if not versao and not atual['copias']:
        return  # Formulário antigo, sem cópias: destinatário segue explícito.
    try:
        anterior = _assinador().loads(versao or '')
    except BadSignature:
        anterior = None
    if anterior != atual:
        raise ValueError('Os e-mails de cópia mudaram ou não foram conferidos. '
                         'Reabra a tela e confirme os destinatários antes de enviar.')

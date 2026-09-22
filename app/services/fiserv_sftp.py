"""Coleta de arquivos Fiserv por SFTP, exclusivamente de leitura.

O servidor precisa constar em um known_hosts provisionado por canal confiável,
na forma ``[host]:6522``. Não descobre nem aceita chaves automaticamente. A chave
privada deve estar em formato suportado pelo Paramiko; arquivos PPK precisam
ser convertidos fora deste serviço. Credenciais e conteúdo nunca entram em logs.

Este módulo não agenda tarefas, grava no banco ou altera arquivos remotos. O
chamador é responsável pela guarda de instância, lock e importação idempotente.

O prazo encerra o transporte SSH, inclusive se a inicialização SFTP bloquear.
Ele não consegue interromper a resolução DNS do sistema operacional nem a
leitura de arquivos locais. Os caminhos de configuração devem apontar para
arquivos regulares locais/protegidos, não FIFOs ou montagens de rede. Após
qualquer dessas etapas, um prazo vencido impede iniciar a operação seguinte.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import posixpath
import socket
import stat
import sys
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import paramiko

HOSTS_PERMITIDOS = frozenset({
    'prod-gw-lac.firstdataclients.com',
    'prod2-gw-lac.firstdataclients.com',
})
HOST_PADRAO = 'prod-gw-lac.firstdataclients.com'
PORTA = 6522
# Caminho absoluto confirmado no acesso Fiserv. SFTP distingue maiúsculas.
PASTA_REMOTA = '/available'
_BLOCO = 64 * 1024
_LOG_TRANSPORTE = __name__ + '.ssh'
_MENSAGEM_PRAZO = 'A coleta Fiserv ultrapassou o tempo máximo permitido.'
_MAX_KNOWN_HOSTS_TEXT = 256 * 1024
_ENV_OBRIGATORIAS = (
    'FISERV_SFTP_USERNAME',
    'FISERV_SFTP_PRIVATE_KEY_PATH',
    'FISERV_SFTP_KNOWN_HOSTS_PATH',
)


class ErroFiservSFTP(Exception):
    """Erro legível que não inclui valores de credenciais nem resposta remota."""


class ErroConfiguracaoFiservSFTP(ErroFiservSFTP):
    def __init__(self, *, faltantes=(), invalidos=()):
        self.faltantes = tuple(faltantes)
        self.invalidos = tuple(invalidos)
        partes = []
        if self.faltantes:
            partes.append('ausentes: ' + ', '.join(self.faltantes))
        if self.invalidos:
            partes.append('revisar: ' + ', '.join(self.invalidos))
        super().__init__('Configuração Fiserv incompleta (' + '; '.join(partes) + ').')


class ErroSegurancaFiservSFTP(ErroFiservSFTP):
    pass


class ErroLimiteFiservSFTP(ErroFiservSFTP):
    pass


class ErroPersistenciaFiservSFTP(ErroFiservSFTP):
    """Falha local ao guardar um arquivo, sem detalhes da operação privada."""

    def __init__(self):
        super().__init__('Não foi possível guardar o arquivo Fiserv recebido.')


class ErroConexaoFiservSFTP(ErroFiservSFTP):
    pass


class ErroAutenticacaoFiservSFTP(ErroConexaoFiservSFTP):
    """Credenciais recusadas: requer revisão, sem retentativa automática de rede."""


class ErroEtapaAutenticacaoFiservSFTP(ErroAutenticacaoFiservSFTP):
    """Etapa e motivo locais; nunca recebe dados do acesso ou texto remoto."""

    def __init__(self, etapa, codigo):
        __tracebackhide__ = True
        etapas = {
            'CHAVE_INICIAL': 'Chave inicial', 'SENHA': 'Etapa de senha',
            'DESAFIO_INTERATIVO': 'Desafio interativo',
            'DESAFIO_APOS_CHAVE': 'Desafio após a confirmação da chave',
            'CHAVE_APOS_SENHA': 'Chave após a senha',
            'SENHA_APOS_CHAVE': 'Senha após a confirmação da chave',
            'CONTINUACAO': 'Continuação da autenticação',
            'CONFIRMACAO_FINAL': 'Confirmação da autenticação',
            'AUTENTICACAO': 'Autenticação',
        }
        codigos = {
            'RECUSADA': 'o servidor não confirmou esta etapa',
            'METODO_NAO_PERMITIDO': 'o servidor não permite este método nesta etapa',
            'SEM_SENHA': 'o servidor solicitou uma etapa de senha, mas nenhuma senha foi informada',
            'DESAFIO_ADICIONAL': 'o servidor solicitou outro desafio após a resposta de senha',
            'DESAFIO_MULTIPLO': 'o servidor solicitou mais de uma resposta no mesmo desafio',
            'CAMPO_VISIVEL': 'o servidor solicitou uma resposta visível; a senha não foi enviada',
            'METODO_NAO_SUPORTADO': 'o servidor solicitou uma continuação não suportada pela coleta',
            'INCOMPLETA': 'o servidor não confirmou a conclusão da autenticação',
        }
        self.etapa = etapa if type(etapa) is str and etapa in etapas else 'AUTENTICACAO'
        self.codigo = codigo if type(codigo) is str and codigo in codigos else 'INCOMPLETA'
        super().__init__(f'{etapas[self.etapa]}: {codigos[self.codigo]}. '
                         f'[{self.etapa}/{self.codigo}] Coleta pausada.')


class ErroOperacaoFiservSFTP(ErroConexaoFiservSFTP):
    """Diagnóstico operacional fechado: não recebe a mensagem do servidor."""

    def __init__(self, etapa, codigo):
        etapas = {
            'CONEXAO': 'conexão SSH', 'ABERTURA_SFTP': 'abertura SFTP',
            'CANAL_SFTP': 'abertura do canal de transferência',
            'SUBSISTEMA_SFTP': 'ativação da transferência',
            'NEGOCIACAO_SFTP': 'negociação da transferência',
            'PASTA_LSTAT': 'consulta da pasta', 'PASTA_REALPATH': 'validação da pasta',
            'LISTAGEM': 'listagem dos arquivos', 'ARQUIVO_LSTAT': 'consulta do arquivo',
            'ARQUIVO_REALPATH': 'validação do arquivo', 'ARQUIVO_OPEN': 'abertura do arquivo',
            'ARQUIVO_FSTAT': 'consulta do arquivo aberto', 'ARQUIVO_READ': 'leitura do arquivo',
            'ENCERRAMENTO': 'encerramento da conexão', 'OPERACAO': 'operação SFTP',
        }
        codigos = {
            'TIMEOUT': 'tempo de resposta excedido', 'DNS': 'endereço não resolvido',
            'EOF': 'conexão encerrada antes da conclusão', 'SSH': 'falha de protocolo SSH',
            'SFTP': 'falha de protocolo SFTP', 'IO_SEM_CODIGO': 'falha sem código SFTP',
            'SFTP_FAILURE': 'resposta de falha do servidor SFTP',
            'SFTP_OP_UNSUPPORTED': 'operação não suportada pelo servidor SFTP',
            'IO_AUSENTE': 'recurso não encontrado', 'IO_CONEXAO': 'conexão interrompida',
            'IO_OUTRO': 'falha de entrada ou saída', 'VALOR_INVALIDO': 'resposta inválida',
            'OUTRO': 'falha não identificada',
        }
        self.etapa = etapa if etapa in etapas else 'OPERACAO'
        self.codigo = codigo if codigo in codigos else 'OUTRO'
        super().__init__(f'Falha na {etapas[self.etapa]}: {codigos[self.codigo]}. '
                         f'[{self.etapa}/{self.codigo}] Nova tentativa em uma hora.')


class ErroAcessoFiservSFTP(ErroFiservSFTP):
    """Falha remota identificada por código fixo, sem texto do servidor."""

    def __init__(self, codigo):
        mensagens = {
            'PASTA_AUSENTE': 'A pasta /available não foi encontrada no acesso Fiserv. Confira a liberação da pasta com a Fiserv.',
            'SEM_PERMISSAO': 'A Fiserv negou permissão para listar ou ler os arquivos. Confira a liberação desse acesso com a Fiserv.',
            'SFTP_INDISPONIVEL': 'A conexão SSH foi estabelecida, mas a Fiserv recusou abrir o acesso SFTP aos arquivos.',
        }
        self.codigo = codigo
        super().__init__(mensagens[codigo])


def _erro_operacao(exc, etapa):
    __tracebackhide__ = True
    paramiko = _carregar_paramiko()
    if isinstance(exc, paramiko.BadHostKeyException):
        return ErroSegurancaFiservSFTP('A chave do servidor Fiserv não corresponde à chave confiável.')
    if isinstance(exc, paramiko.AuthenticationException):
        return ErroAutenticacaoFiservSFTP('A autenticação SFTP Fiserv foi recusada; revise as credenciais.')
    if isinstance(exc, OSError) and etapa not in (
            'CONEXAO', 'ABERTURA_SFTP', 'CANAL_SFTP', 'SUBSISTEMA_SFTP', 'NEGOCIACAO_SFTP'):
        if exc.errno in (errno.EACCES, errno.EPERM):
            return ErroAcessoFiservSFTP('SEM_PERMISSAO')
        if etapa.startswith('PASTA_') and exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return ErroAcessoFiservSFTP('PASTA_AUSENTE')
    if isinstance(exc, socket.gaierror):
        codigo = 'DNS'
    elif isinstance(exc, TimeoutError):
        codigo = 'TIMEOUT'
    elif isinstance(exc, EOFError):
        codigo = 'EOF'
    elif isinstance(exc, paramiko.SFTPError):
        codigo = 'SFTP'
    elif isinstance(exc, paramiko.SSHException):
        codigo = 'SSH'
    elif isinstance(exc, _ErroStatusSFTP):
        codigo = {4: 'SFTP_FAILURE', 8: 'SFTP_OP_UNSUPPORTED'}.get(exc.codigo_status, 'SFTP')
    elif isinstance(exc, OSError):
        if exc.errno is None:
            codigo = 'IO_SEM_CODIGO'
        elif exc.errno in (errno.ENOENT, errno.ENOTDIR):
            codigo = 'IO_AUSENTE'
        elif exc.errno in (errno.ECONNRESET, errno.ECONNREFUSED, errno.ECONNABORTED,
                           errno.EPIPE, errno.ENETUNREACH, errno.EHOSTUNREACH):
            codigo = 'IO_CONEXAO'
        else:
            codigo = 'IO_OUTRO'
    elif isinstance(exc, ValueError):
        codigo = 'VALOR_INVALIDO'
    else:
        codigo = 'OUTRO'
    return ErroOperacaoFiservSFTP(etapa, codigo)


def _executar_sftp(etapa, operacao, *args, **kwargs):
    __tracebackhide__ = True
    paramiko = _carregar_paramiko()
    try:
        return operacao(*args, **kwargs)
    except ErroFiservSFTP:
        raise
    except (OSError, ValueError, EOFError, paramiko.SSHException, paramiko.SFTPError) as exc:
        raise _erro_operacao(exc, etapa) from None


class _ErroStatusSFTP(OSError):
    """Preserva somente o código do protocolo, nunca o texto remoto."""

    def __init__(self, codigo):
        self.codigo_status = codigo
        super().__init__('O servidor retornou uma falha SFTP.')


def _cliente_sftp(canal):
    from paramiko import SFTPClient

    class ClienteSFTPComStatus(SFTPClient):
        def _convert_status(self, mensagem):
            __tracebackhide__ = True
            codigo = mensagem.get_int()
            # Paramiko transforma tanto FAILURE (4) quanto OP_UNSUPPORTED (8)
            # em IOError(texto). Preserve o código sem carregar o texto privado.
            if codigo == 0:
                return
            if codigo == 1:
                raise EOFError('Fim do arquivo remoto.')
            if codigo == 2:
                raise OSError(errno.ENOENT, 'Recurso remoto não encontrado.')
            if codigo == 3:
                raise OSError(errno.EACCES, 'Permissão remota negada.')
            raise _ErroStatusSFTP(codigo)

    return ClienteSFTPComStatus(canal)


@dataclass(frozen=True, repr=False, init=False)
class ConfigFiservSFTP:
    usuario: str
    chave_privada_path: str | None
    known_hosts_path: str | None
    chave_privada: paramiko.PKey | None = field(repr=False)
    known_hosts_text: str | None = field(repr=False)
    senha: str | None = field(repr=False)
    host: str
    passphrase: str | None = field(repr=False)
    passphrase_path: str | None = field(repr=False)
    timeout_segundos: int
    duracao_maxima_segundos: int
    max_arquivos: int
    max_bytes_arquivo: int
    max_bytes_total: int

    def __init__(
        self, usuario: str, chave_privada_path: str | None = None,
        known_hosts_path: str | None = None,
        host: str = HOST_PADRAO, passphrase: str | None = None,
        passphrase_path: str | None = None, timeout_segundos: int = 20,
        duracao_maxima_segundos: int = 120, max_arquivos: int = 100,
        max_bytes_arquivo: int = 16 * 1024 * 1024,
        max_bytes_total: int = 64 * 1024 * 1024,
        *, chave_privada: paramiko.PKey | None = None,
        known_hosts_text: str | None = None, senha: str | None = None,
    ):
        # O __init__ gerado pela dataclass exporia os argumentos ao Sentry
        # quando a validação falha. O construtor explícito protege esse frame.
        __tracebackhide__ = True
        for nome, valor in (
            ('usuario', usuario), ('chave_privada_path', chave_privada_path),
            ('known_hosts_path', known_hosts_path), ('host', host),
            ('chave_privada', chave_privada), ('known_hosts_text', known_hosts_text),
            ('senha', senha),
            ('passphrase', passphrase), ('passphrase_path', passphrase_path),
            ('timeout_segundos', timeout_segundos),
            ('duracao_maxima_segundos', duracao_maxima_segundos),
            ('max_arquivos', max_arquivos), ('max_bytes_arquivo', max_bytes_arquivo),
            ('max_bytes_total', max_bytes_total),
        ):
            object.__setattr__(self, nome, valor)
        self.__post_init__()

    def __post_init__(self):
        __tracebackhide__ = True  # Sentry não deve serializar campos de configuração.
        invalidos = []
        if not isinstance(self.usuario, str) or not self.usuario.strip() or '\x00' in self.usuario:
            invalidos.append('FISERV_SFTP_USERNAME')
        for nome, valor in (
            ('FISERV_SFTP_PRIVATE_KEY_PATH', self.chave_privada_path),
            ('FISERV_SFTP_KNOWN_HOSTS_PATH', self.known_hosts_path),
        ):
            if valor is None:
                continue
            if not isinstance(valor, str) or not valor.strip() or '\x00' in valor:
                invalidos.append(nome)
        if (self.chave_privada is None) == (self.chave_privada_path is None):
            invalidos.extend(('chave_privada', 'FISERV_SFTP_PRIVATE_KEY_PATH'))
        if (self.known_hosts_text is None) == (self.known_hosts_path is None):
            invalidos.extend(('known_hosts_text', 'FISERV_SFTP_KNOWN_HOSTS_PATH'))
        if self.chave_privada is not None:
            modulo_paramiko = _carregar_paramiko()
            if not isinstance(self.chave_privada, modulo_paramiko.PKey) or not self.chave_privada.can_sign():
                invalidos.append('chave_privada')
        if self.known_hosts_text is not None and (
            not isinstance(self.known_hosts_text, str) or not self.known_hosts_text.strip()
            or len(self.known_hosts_text) > _MAX_KNOWN_HOSTS_TEXT or '\x00' in self.known_hosts_text
        ):
            invalidos.append('known_hosts_text')
        if self.senha is not None and not isinstance(self.senha, str):
            invalidos.append('FISERV_SFTP_PASSWORD')
        if self.host not in HOSTS_PERMITIDOS:
            invalidos.append('FISERV_SFTP_HOST')
        if self.passphrase is not None and not isinstance(self.passphrase, str):
            invalidos.append('FISERV_SFTP_KEY_PASSPHRASE')
        if self.passphrase_path is not None and (
            not isinstance(self.passphrase_path, str)
            or not self.passphrase_path.strip() or '\x00' in self.passphrase_path
        ):
            invalidos.append('FISERV_SFTP_KEY_PASSPHRASE_PATH')
        if self.passphrase is not None and self.passphrase_path is not None:
            invalidos.extend(('FISERV_SFTP_KEY_PASSPHRASE', 'FISERV_SFTP_KEY_PASSPHRASE_PATH'))
        for nome, valor, teto in (
            ('timeout_segundos', self.timeout_segundos, 60),
            ('duracao_maxima_segundos', self.duracao_maxima_segundos, 600),
            ('max_arquivos', self.max_arquivos, 10000),
            ('max_bytes_arquivo', self.max_bytes_arquivo, 64 * 1024 * 1024),
            ('max_bytes_total', self.max_bytes_total, 256 * 1024 * 1024),
        ):
            if type(valor) is not int or not 1 <= valor <= teto:
                invalidos.append(nome)
        if invalidos:
            raise ErroConfiguracaoFiservSFTP(invalidos=invalidos)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None):
        __tracebackhide__ = True  # O mapping contém credenciais de integração.
        env = os.environ if environ is None else environ
        faltantes = [nome for nome in _ENV_OBRIGATORIAS if not (env.get(nome) or '').strip()]
        if faltantes:
            raise ErroConfiguracaoFiservSFTP(faltantes=faltantes)
        return cls(
            usuario=env['FISERV_SFTP_USERNAME'].strip(),
            chave_privada_path=env['FISERV_SFTP_PRIVATE_KEY_PATH'].strip(),
            known_hosts_path=env['FISERV_SFTP_KNOWN_HOSTS_PATH'].strip(),
            host=(env.get('FISERV_SFTP_HOST') or HOST_PADRAO).strip(),
            passphrase=env.get('FISERV_SFTP_KEY_PASSPHRASE') or None,
            passphrase_path=(env.get('FISERV_SFTP_KEY_PASSPHRASE_PATH') or '').strip() or None,
            senha=env.get('FISERV_SFTP_PASSWORD') or None,
        )


@dataclass(frozen=True)
class MetadadosArquivoFiserv:
    nome: str
    tamanho: int
    modificado_em: int


@dataclass(frozen=True)
class ArquivoFiserv:
    metadados: MetadadosArquivoFiserv
    conteudo: bytes = field(repr=False)
    sha256: str


def status_configuracao(environ: Mapping[str, str] | None = None):
    """Status declarativo: somente nomes de opções, sem ler segredos ou usar rede.

    ``configurado`` significa variáveis coerentes; não atesta arquivos locais,
    dependência instalada, autenticação ou a chave do servidor.
    """
    __tracebackhide__ = True
    try:
        ConfigFiservSFTP.from_env(environ)
    except ErroConfiguracaoFiservSFTP as exc:
        return {'configurado': False, 'faltantes': list(exc.faltantes), 'invalidos': list(exc.invalidos)}
    return {'configurado': True, 'faltantes': [], 'invalidos': []}


def _carregar_paramiko():
    try:
        import paramiko
    except ImportError:
        raise ErroConfiguracaoFiservSFTP(invalidos=('dependência Paramiko',)) from None
    return paramiko


def _passphrase(config):
    __tracebackhide__ = True  # Oculta bytes/frase também de captura de locais do Sentry.
    if config.passphrase_path is None:
        return config.passphrase
    try:
        with Path(config.passphrase_path).open('rb') as arquivo:
            valor = arquivo.read(16 * 1024 + 1)
        if len(valor) > 16 * 1024:
            raise ValueError
        return valor.decode('utf-8').rstrip('\r\n')
    except (OSError, ValueError):
        raise ErroConfiguracaoFiservSFTP(invalidos=('FISERV_SFTP_KEY_PASSPHRASE_PATH',)) from None


def _carregar_host_keys(cliente, config, modulo_paramiko):
    """Carrega confiança provisionada; texto só autoriza o host/porta escolhidos."""
    __tracebackhide__ = True
    nome_opcao = 'known_hosts_text' if config.known_hosts_text is not None else 'FISERV_SFTP_KNOWN_HOSTS_PATH'
    host_exato = f'[{config.host}]:{PORTA}'
    try:
        if config.known_hosts_text is None:
            cliente.load_host_keys(config.known_hosts_path)
        else:
            chaves = cliente.get_host_keys()
            for numero, linha in enumerate(config.known_hosts_text.splitlines(), start=1):
                linha = linha.strip()
                if not linha or linha.startswith('#'):
                    continue
                entrada = modulo_paramiko.hostkeys.HostKeyEntry.from_line(linha, lineno=numero)
                if entrada is None or entrada.key is None:
                    raise ValueError
                if host_exato not in entrada.hostnames:
                    continue
                tipo = entrada.key.get_name()
                existentes = chaves.lookup(host_exato)
                if existentes and tipo in existentes and existentes[tipo] != entrada.key:
                    raise ValueError  # Duas chaves diferentes do mesmo tipo são ambíguas.
                chaves.add(host_exato, tipo, entrada.key)
        if not cliente.get_host_keys().lookup(host_exato):
            raise ValueError
    except (OSError, ValueError, modulo_paramiko.SSHException, modulo_paramiko.hostkeys.InvalidHostKey):
        raise ErroConfiguracaoFiservSFTP(invalidos=(nome_opcao,)) from None


def _prazo_restante(config, prazo):
    restante = prazo - time.monotonic()
    if restante <= 0:
        raise ErroLimiteFiservSFTP(_MENSAGEM_PRAZO)
    return min(config.timeout_segundos, restante)


def _timeout(sftp, config, prazo):
    sftp.get_channel().settimeout(_prazo_restante(config, prazo))


def _transporte_autenticacao_unica(config, prazo):
    """Negocia ssh-userauth uma vez, preservando os fatores já aceitos.

    O Transport legado repete SERVICE_REQUEST a cada fator. Alguns servidores
    reiniciam a autenticação com isso. O transporte alternativo do Paramiko
    evita a repetição, mas sua espera pelo serviço precisa de prazo e seu
    handler precisa armar o evento antes do envio da solicitação.
    """
    from paramiko.auth_handler import AuthOnlyHandler
    from paramiko.common import cMSG_SERVICE_REQUEST, cMSG_USERAUTH_REQUEST
    from paramiko.message import Message
    from paramiko.transport import ServiceRequestingTransport

    class AutenticacaoComEvento(AuthOnlyHandler):
        def send_auth_request(self, username, method, finish_message=None):
            __tracebackhide__ = True
            self.auth_method = method
            self.username = username
            mensagem = Message()
            mensagem.add_byte(cMSG_USERAUTH_REQUEST)
            mensagem.add_string(username)
            mensagem.add_string('ssh-connection')
            mensagem.add_string(method)
            if finish_message is not None:
                finish_message(mensagem)
            # Uma resposta pode chegar antes de _send_message retornar.
            self.auth_event = threading.Event()
            with self.transport.lock:
                self.transport._send_message(mensagem)
            return self.wait_for_response(self.auth_event)

    class TransporteAutenticacaoUnica(ServiceRequestingTransport):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._fiserv_servico_solicitado = False
            self._fiserv_servico_aceito = threading.Event()

        def _parse_service_accept(self, mensagem):
            __tracebackhide__ = True
            super()._parse_service_accept(mensagem)
            if self._service_userauth_accepted:
                self._fiserv_servico_aceito.set()

        def get_auth_handler(self):
            return AutenticacaoComEvento(self)

        def ensure_session(self):
            __tracebackhide__ = True
            _prazo_restante(config, prazo)
            if not self.is_active() or not self.initial_kex_done:
                raise EOFError('Sessão SSH indisponível.')
            if self._service_userauth_accepted and self.auth_handler is not None:
                self.auth_handler = self.get_auth_handler()
                return
            limite = min(prazo, time.monotonic() + (self.auth_timeout or config.timeout_segundos))
            if not self._fiserv_servico_solicitado:
                self._fiserv_servico_solicitado = True
                mensagem = Message()
                mensagem.add_byte(cMSG_SERVICE_REQUEST)
                mensagem.add_string('ssh-userauth')
                self._send_message(mensagem)
            while not self._service_userauth_accepted:
                if not self.is_active():
                    raise EOFError('Sessão SSH encerrada durante a autenticação.')
                restante = min(_prazo_restante(config, prazo), limite - time.monotonic())
                if restante <= 0:
                    raise socket.timeout('Serviço de autenticação não respondeu no prazo.')
                self._fiserv_servico_aceito.wait(min(0.1, restante))
            _prazo_restante(config, prazo)
            if not self.is_active():
                raise EOFError('Sessão SSH encerrada durante a autenticação.')
            self.auth_handler = self.get_auth_handler()

    return TransporteAutenticacaoUnica


class _AutenticacaoEmMemoria:
    """Autenticação finita, inclusive quando a senha precisa vir antes da chave."""

    def __init__(self, config, prazo):
        self._config = config
        self._prazo = prazo

    def authenticate(self, transport):
        __tracebackhide__ = True
        paramiko = _carregar_paramiko()
        config = self._config

        def autenticar_chave():
            __tracebackhide__ = True
            transport.auth_timeout = _prazo_restante(config, self._prazo)
            return transport.auth_publickey(config.usuario, config.chave_privada)

        def autenticado():
            __tracebackhide__ = True
            if not transport.is_active():
                raise ErroOperacaoFiservSFTP('CONEXAO', 'EOF')
            return transport.is_authenticated()

        def autenticar_senha(metodos, etapa='SENHA'):
            __tracebackhide__ = True
            respondeu = False
            etapa_desafio = 'DESAFIO_APOS_CHAVE' if etapa == 'SENHA_APOS_CHAVE' else 'DESAFIO_INTERATIVO'

            def responder(_titulo, _instrucoes, campos):
                __tracebackhide__ = True
                nonlocal respondeu
                if not campos:
                    return []
                # Uma resposta de senha, nunca desafios adicionais/OTP nem stdin.
                if respondeu:
                    raise ErroEtapaAutenticacaoFiservSFTP(etapa_desafio, 'DESAFIO_ADICIONAL')
                if len(campos) != 1:
                    raise ErroEtapaAutenticacaoFiservSFTP(etapa_desafio, 'DESAFIO_MULTIPLO')
                if campos[0][1]:
                    raise ErroEtapaAutenticacaoFiservSFTP(etapa_desafio, 'CAMPO_VISIVEL')
                respondeu = True
                return [config.senha]

            transport.auth_timeout = _prazo_restante(config, self._prazo)
            if 'password' in metodos:
                try:
                    return transport.auth_password(config.usuario, config.senha, fallback=False)
                except paramiko.BadAuthenticationType as exc:
                    if 'keyboard-interactive' not in exc.allowed_types:
                        raise ErroEtapaAutenticacaoFiservSFTP(etapa, 'METODO_NAO_PERMITIDO') from None
                except paramiko.AuthenticationException:
                    raise ErroEtapaAutenticacaoFiservSFTP(etapa, 'RECUSADA') from None
            transport.auth_timeout = _prazo_restante(config, self._prazo)
            try:
                return transport.auth_interactive(config.usuario, responder)
            except paramiko.BadAuthenticationType:
                raise ErroEtapaAutenticacaoFiservSFTP(etapa_desafio, 'METODO_NAO_PERMITIDO') from None
            except paramiko.AuthenticationException:
                raise ErroEtapaAutenticacaoFiservSFTP(etapa_desafio, 'RECUSADA') from None

        try:
            proximos = autenticar_chave()
        except paramiko.BadAuthenticationType as exc:
            proximos = exc.allowed_types
            if not any(metodo in proximos for metodo in ('password', 'keyboard-interactive')):
                raise ErroEtapaAutenticacaoFiservSFTP('CHAVE_INICIAL', 'METODO_NAO_PERMITIDO') from None
            if config.senha is None:
                raise ErroEtapaAutenticacaoFiservSFTP('CONTINUACAO', 'SEM_SENHA') from None
        except paramiko.AuthenticationException:
            if config.senha is None:
                raise ErroEtapaAutenticacaoFiservSFTP('CHAVE_INICIAL', 'RECUSADA') from None
            # Alguns servidores só aceitam a chave depois da senha.
            proximos = ('password',)
        if autenticado():
            return
        if not any(metodo in proximos for metodo in ('password', 'keyboard-interactive')):
            codigo = 'METODO_NAO_SUPORTADO' if proximos else 'INCOMPLETA'
            raise ErroEtapaAutenticacaoFiservSFTP('CONTINUACAO', codigo)
        if config.senha is None:
            raise ErroEtapaAutenticacaoFiservSFTP('CONTINUACAO', 'SEM_SENHA')
        proximos = autenticar_senha(proximos)
        if autenticado():
            return
        # A API pode retornar sucesso parcial sem lançar exceção. Completa
        # somente a continuação publickey solicitada pelo próprio servidor.
        if 'publickey' in proximos:
            try:
                proximos = autenticar_chave()
            except paramiko.BadAuthenticationType:
                raise ErroEtapaAutenticacaoFiservSFTP('CHAVE_APOS_SENHA', 'METODO_NAO_PERMITIDO') from None
            except paramiko.AuthenticationException:
                raise ErroEtapaAutenticacaoFiservSFTP('CHAVE_APOS_SENHA', 'RECUSADA') from None
        else:
            codigo = 'METODO_NAO_SUPORTADO' if proximos else 'INCOMPLETA'
            raise ErroEtapaAutenticacaoFiservSFTP('CONTINUACAO', codigo)
        if autenticado():
            return
        if not any(metodo in proximos for metodo in ('password', 'keyboard-interactive')):
            codigo = 'METODO_NAO_SUPORTADO' if proximos else 'INCOMPLETA'
            raise ErroEtapaAutenticacaoFiservSFTP('CHAVE_APOS_SENHA', codigo)
        # A chave também pode ter sido aceita parcialmente. Atende uma única
        # continuação de senha explicitamente solicitada após essa etapa; nunca
        # repete uma senha recusada nem inicia outro ciclo de autenticação.
        autenticar_senha(proximos, etapa='SENHA_APOS_CHAVE')
        if not autenticado():
            raise ErroEtapaAutenticacaoFiservSFTP('SENHA_APOS_CHAVE', 'INCOMPLETA')


@contextmanager
def _conectar(config):
    __tracebackhide__ = True
    paramiko = _carregar_paramiko()
    cliente = None
    sftp = None
    canal = None
    etapa = 'CONEXAO'
    temporizador = None
    prazo_expirado = threading.Event()
    trava_fechamento = threading.Lock()
    prazo = time.monotonic() + config.duracao_maxima_segundos

    def fechar_cliente():
        __tracebackhide__ = True
        # SSHClient.close altera _transport; chamadas concorrentes precisam
        # ser serializadas para a expiração não disputar com o finally.
        with trava_fechamento:
            if cliente is not None:
                cliente.close()

    def expirar():
        __tracebackhide__ = True
        prazo_expirado.set()
        try:
            with trava_fechamento:
                transporte = cliente.get_transport() if cliente is not None else None
                if transporte is not None:
                    # Não usar SSHClient.close aqui: ele zera _transport,
                    # ainda consultado por _auth enquanto connect está ativo.
                    transporte.close()
        except (OSError, EOFError, ValueError, paramiko.SSHException, paramiko.SFTPError):
            # A causa pública continua sendo o prazo vencido. Não imprimir
            # exceção de thread que possa conter resposta privada do servidor.
            return

    def verificar_prazo():
        __tracebackhide__ = True
        if prazo_expirado.is_set():
            raise ErroLimiteFiservSFTP(_MENSAGEM_PRAZO)
        return _prazo_restante(config, prazo)

    try:
        cliente = paramiko.SSHClient()
        # O DEBUG do Paramiko inclui caminhos de chaves e nomes remotos. Este
        # canal privado não propaga nem emite esses detalhes; erros operacionais
        # continuam visíveis nas exceções sanitizadas retornadas abaixo.
        log_transporte = logging.getLogger(_LOG_TRANSPORTE)
        log_transporte.setLevel(logging.CRITICAL + 1)
        log_transporte.propagate = False
        cliente.set_log_channel(_LOG_TRANSPORTE)
        cliente.set_missing_host_key_policy(paramiko.RejectPolicy())
        temporizador = threading.Timer(config.duracao_maxima_segundos, expirar)
        temporizador.daemon = True
        temporizador.start()
        _carregar_host_keys(cliente, config, paramiko)
        frase = _passphrase(config)
        # Paramiko reutiliza password para decifrar a chave quando passphrase
        # é None. Uma string vazia impede essa mistura de segredos distintos.
        if config.senha is not None and frase is None:
            frase = ''
        timeout = verificar_prazo()
        if config.chave_privada is not None:
            autenticacao = {
                'auth_strategy': _AutenticacaoEmMemoria(config, prazo),
                'transport_factory': _transporte_autenticacao_unica(config, prazo),
            }
        else:
            autenticacao = {'key_filename': config.chave_privada_path, 'passphrase': frase}
            if config.senha is not None:
                autenticacao['password'] = config.senha
        cliente.connect(
            hostname=config.host, port=PORTA, username=config.usuario,
            **autenticacao,
            allow_agent=False, look_for_keys=False,
            timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
            channel_timeout=timeout,
        )
        # O timer pode ter vencido durante DNS/chave, antes de haver transporte
        # para fechar; nesse caso não abre SFTP com um timer já consumido.
        verificar_prazo()
        transporte = cliente.get_transport()
        if transporte is None or not transporte.is_active():
            raise ErroOperacaoFiservSFTP('CONEXAO', 'EOF')
        if not transporte.is_authenticated():
            raise ErroEtapaAutenticacaoFiservSFTP('CONFIRMACAO_FINAL', 'INCOMPLETA')
        etapa = 'CANAL_SFTP'
        canal = transporte.open_session(timeout=verificar_prazo())
        canal.settimeout(verificar_prazo())
        etapa = 'SUBSISTEMA_SFTP'
        canal.invoke_subsystem('sftp')
        canal.settimeout(verificar_prazo())
        etapa = 'NEGOCIACAO_SFTP'
        sftp = _cliente_sftp(canal)
        etapa = 'OPERACAO'
        verificar_prazo()
        _timeout(sftp, config, prazo)
        yield sftp, prazo
        verificar_prazo()
    except (ErroOperacaoFiservSFTP, ErroEtapaAutenticacaoFiservSFTP):
        if prazo_expirado.is_set():
            raise ErroLimiteFiservSFTP(_MENSAGEM_PRAZO) from None
        raise
    except ErroFiservSFTP:
        raise
    except (OSError, ValueError, EOFError, paramiko.SSHException, paramiko.SFTPError) as exc:
        if prazo_expirado.is_set():
            raise ErroLimiteFiservSFTP(_MENSAGEM_PRAZO) from None
        # Recusa administrativa/tipo de canal desconhecido exigem revisão.
        # Falha de conexão ou falta de recursos (2/4) continuam transitórias.
        if (etapa == 'CANAL_SFTP' and isinstance(exc, paramiko.ChannelException)
                and exc.code in (1, 3)):
            raise ErroAcessoFiservSFTP('SFTP_INDISPONIVEL') from None
        raise _erro_operacao(exc, etapa) from None
    finally:
        # Fechar ambos mesmo se um falhar; nunca expor mensagens do transporte.
        # Uma falha de encerramento não substitui a causa original da falha.
        erro_em_curso = sys.exc_info()[0] is not None
        erro_fechamento = False
        try:
            if sftp is not None:
                try:
                    sftp.close()
                except (OSError, EOFError, ValueError, paramiko.SSHException, paramiko.SFTPError):
                    erro_fechamento = True
            elif canal is not None:
                try:
                    canal.close()
                except (OSError, EOFError, ValueError, paramiko.SSHException, paramiko.SFTPError):
                    erro_fechamento = True
            try:
                fechar_cliente()
            except (OSError, EOFError, ValueError, paramiko.SSHException, paramiko.SFTPError):
                erro_fechamento = True
        finally:
            if temporizador is not None:
                temporizador.cancel()
                temporizador.join()
        if prazo_expirado.is_set() and not erro_em_curso:
            raise ErroLimiteFiservSFTP(_MENSAGEM_PRAZO) from None
        if erro_fechamento and not erro_em_curso:
            raise ErroOperacaoFiservSFTP('ENCERRAMENTO', 'OUTRO') from None


def _nome_seguro(nome):
    if (
        not isinstance(nome, str) or not nome or len(nome) > 255
        or nome in ('.', '..') or any(c in nome for c in '/\\:')
        or any(ord(c) < 32 or ord(c) == 127 for c in nome)
    ):
        raise ErroSegurancaFiservSFTP('Nome de arquivo remoto inválido.')
    return nome


def _pasta_disponivel(sftp, config, prazo):
    __tracebackhide__ = True
    _timeout(sftp, config, prazo)
    atributos = _executar_sftp('PASTA_LSTAT', sftp.lstat, PASTA_REMOTA)
    if not isinstance(atributos.st_mode, int) or not stat.S_ISDIR(atributos.st_mode):
        raise ErroSegurancaFiservSFTP('/available precisa ser um diretório real, sem link simbólico.')
    _timeout(sftp, config, prazo)
    if _executar_sftp('PASTA_REALPATH', sftp.normalize, PASTA_REMOTA) != PASTA_REMOTA:
        raise ErroSegurancaFiservSFTP('/available não pode redirecionar para outro diretório.')
    return PASTA_REMOTA


def _metadados(nome, atributos, config):
    _nome_seguro(nome)
    if not isinstance(atributos.st_mode, int) or not stat.S_ISREG(atributos.st_mode):
        raise ErroSegurancaFiservSFTP('A coleta aceita somente arquivos regulares, sem links simbólicos.')
    tamanho = atributos.st_size
    mtime = atributos.st_mtime
    if type(tamanho) is not int or tamanho < 0 or type(mtime) is not int or mtime < 0:
        raise ErroSegurancaFiservSFTP('Metadados de arquivo remoto inválidos.')
    if tamanho > config.max_bytes_arquivo:
        raise ErroLimiteFiservSFTP('Arquivo Fiserv excede o limite de tamanho permitido.')
    return MetadadosArquivoFiserv(nome=nome, tamanho=tamanho, modificado_em=mtime)


def _listar(sftp, pasta, config, prazo):
    __tracebackhide__ = True
    paramiko = _carregar_paramiko()
    arquivos = []
    nomes = set()
    _timeout(sftp, config, prazo)
    entradas = _executar_sftp('LISTAGEM', sftp.listdir_iter, pasta, read_aheads=1)
    try:
        for indice, atributos in enumerate(entradas):
            _timeout(sftp, config, prazo)
            if indice >= config.max_arquivos:
                raise ErroLimiteFiservSFTP('/available excede o limite de entradas por coleta.')
            nome = _nome_seguro(atributos.filename)
            if nome in nomes:
                raise ErroSegurancaFiservSFTP('A listagem Fiserv contém nomes repetidos.')
            nomes.add(nome)
            if isinstance(atributos.st_mode, int) and stat.S_ISDIR(atributos.st_mode):
                continue  # Não percorre subdiretórios; eles também contam para o limite.
            arquivos.append(_metadados(nome, atributos, config))
    except ErroFiservSFTP:
        raise
    except (OSError, ValueError, EOFError, paramiko.SSHException, paramiko.SFTPError) as exc:
        raise _erro_operacao(exc, 'LISTAGEM') from None
    finally:
        entradas.close()
    _prazo_restante(config, prazo)
    return sorted(arquivos, key=lambda arquivo: arquivo.nome)


def _verificar_arquivo(sftp, caminho, nome, config, prazo, *, permitir_ausente=False):
    __tracebackhide__ = True
    ausente = object()

    def consultar(operacao):
        __tracebackhide__ = True
        try:
            return operacao(caminho)
        except OSError as exc:
            # Um handle já aberto pode continuar legível após rename/unlink.
            # ENOTDIR, permissão e rede não equivalem à ausência do caminho.
            if permitir_ausente and exc.errno == errno.ENOENT:
                return ausente
            raise

    _timeout(sftp, config, prazo)
    atributos = _executar_sftp('ARQUIVO_LSTAT', consultar, sftp.lstat)
    if atributos is ausente:
        return None
    metadados = _metadados(nome, atributos, config)
    _timeout(sftp, config, prazo)
    normalizado = _executar_sftp('ARQUIVO_REALPATH', consultar, sftp.normalize)
    if normalizado is ausente:
        return metadados
    if normalizado != caminho:
        raise ErroSegurancaFiservSFTP('Arquivo remoto não pode redirecionar para outro caminho.')
    return metadados


def _metadados_arquivo_aberto(remoto, nome, config):
    __tracebackhide__ = True
    indisponivel = object()

    def consultar():
        __tracebackhide__ = True
        try:
            return remoto.stat()
        except _ErroStatusSFTP as exc:
            # Alguns servidores permitem OPEN/READ, mas não FSTAT. FAILURE
            # não prova falta de suporte: apenas permite conferir pelo caminho.
            # Outros códigos, desconexão ou erros genéricos nunca são ignorados.
            if exc.codigo_status in (4, 8):
                return indisponivel
            raise

    atributos = _executar_sftp('ARQUIVO_FSTAT', consultar)
    return None if atributos is indisponivel else _metadados(nome, atributos, config)


@contextmanager
def _arquivo_remoto(sftp, caminho):
    __tracebackhide__ = True
    remoto = _executar_sftp('ARQUIVO_OPEN', sftp.open, caminho, 'rb', bufsize=0)
    try:
        yield remoto
    finally:
        erro_em_curso = sys.exc_info()[0] is not None
        try:
            _executar_sftp('ENCERRAMENTO', remoto.close)
        except ErroFiservSFTP:
            if not erro_em_curso:
                raise
        except Exception:
            if not erro_em_curso:
                raise ErroOperacaoFiservSFTP('ENCERRAMENTO', 'OUTRO') from None


def _baixar(sftp, pasta, nome, config, prazo, esperado=None, *, ao_receber=None):
    __tracebackhide__ = True  # Não enviar conteúdo bancário em eventos de exceção.
    caminho = posixpath.join(pasta, _nome_seguro(nome))
    antes = _verificar_arquivo(sftp, caminho, nome, config, prazo)
    if antes.tamanho > config.max_bytes_total:
        raise ErroLimiteFiservSFTP('Arquivo Fiserv excede o limite total de bytes por coleta.')
    if esperado is not None and antes != esperado:
        raise ErroSegurancaFiservSFTP('Arquivo Fiserv mudou após a listagem; tente novamente.')
    conteudo = bytearray()
    _timeout(sftp, config, prazo)
    with _arquivo_remoto(sftp, caminho) as remoto:
        _timeout(sftp, config, prazo)
        aberto = _metadados_arquivo_aberto(remoto, nome, config)
        caminho_aberto = _verificar_arquivo(sftp, caminho, nome, config, prazo, permitir_ausente=True)
        if ((aberto is not None and aberto != antes)
                or (caminho_aberto is not None and caminho_aberto != antes)):
            raise ErroSegurancaFiservSFTP('Arquivo Fiserv mudou antes da leitura; tente novamente.')
        # Caixas de entrega podem invalidar o handle ao entregar o último byte.
        # Não pedir além do tamanho anunciado: BufferedFile.read tentaria
        # completar esse pedido extra e perderia o bloco recebido se a resposta
        # seguinte fosse ENOENT, em vez de EOF. Erros durante bytes esperados
        # continuam sendo falhas; metadados observáveis são conferidos abaixo.
        while len(conteudo) < antes.tamanho:
            _timeout(sftp, config, prazo)
            bloco = _executar_sftp('ARQUIVO_READ', remoto.read, min(_BLOCO, antes.tamanho - len(conteudo)))
            if not bloco:
                raise ErroSegurancaFiservSFTP('Arquivo Fiserv terminou antes do tamanho informado.')
            conteudo.extend(bloco)
            if len(conteudo) > antes.tamanho or len(conteudo) > config.max_bytes_arquivo:
                raise ErroLimiteFiservSFTP('Arquivo Fiserv excedeu o tamanho informado durante a leitura.')
        _timeout(sftp, config, prazo)
        depois = _metadados_arquivo_aberto(remoto, nome, config) if aberto is not None else None
        # Conferir enquanto o handle está aberto: caixas de arquivos podem
        # retirar o caminho durante a leitura ou quando o download é encerrado.
        # SFTP v3 não oferece OPEN_NOFOLLOW; detectamos trocas observáveis, sem
        # autenticar o conteúdo de um servidor comprometido. O pin é obrigatório.
        final = _verificar_arquivo(sftp, caminho, nome, config, prazo, permitir_ausente=True)
        if (len(conteudo) != antes.tamanho or (depois is not None and depois != antes)
                or (final is not None and final != antes)):
            raise ErroSegurancaFiservSFTP('Arquivo Fiserv mudou durante a leitura; tente novamente.')
        dados = bytes(conteudo)
        arquivo = ArquivoFiserv(metadados=antes, conteudo=dados, sha256=hashlib.sha256(dados).hexdigest())
        if ao_receber is not None:
            try:
                # Guarda durável por arquivo antes do CLOSE normal, sem esperar
                # que todos os demais downloads do lote também terminem.
                ao_receber(arquivo)
            except Exception:
                raise ErroPersistenciaFiservSFTP() from None
    return arquivo


def listar_arquivos(config: ConfigFiservSFTP | None = None) -> list[MetadadosArquivoFiserv]:
    """Lista arquivos regulares diretamente em /available, sem baixar conteúdo."""
    __tracebackhide__ = True
    config = config or ConfigFiservSFTP.from_env()
    with _conectar(config) as (sftp, prazo):
        return _listar(sftp, _pasta_disponivel(sftp, config, prazo), config, prazo)


def baixar_arquivo(nome: str, config: ConfigFiservSFTP | None = None) -> ArquivoFiserv:
    """Baixa um nome simples de /available para memória; não grava nem remove."""
    __tracebackhide__ = True
    _nome_seguro(nome)
    config = config or ConfigFiservSFTP.from_env()
    with _conectar(config) as (sftp, prazo):
        return _baixar(sftp, _pasta_disponivel(sftp, config, prazo), nome, config, prazo)


def coletar_arquivos(config: ConfigFiservSFTP | None = None) -> list[ArquivoFiserv]:
    """Lista e baixa um lote com limites de quantidade, bytes e duração total.

    Em falha, não devolve lote parcial. Arquivos remotos permanecem intactos.
    """
    __tracebackhide__ = True
    config = config or ConfigFiservSFTP.from_env()
    with _conectar(config) as (sftp, prazo):
        pasta = _pasta_disponivel(sftp, config, prazo)
        metadados = _listar(sftp, pasta, config, prazo)
        if sum(item.tamanho for item in metadados) > config.max_bytes_total:
            raise ErroLimiteFiservSFTP('O lote Fiserv excede o limite total de bytes por coleta.')
        return [_baixar(sftp, pasta, item.nome, config, prazo, esperado=item) for item in metadados]


def coletar_lote_pendente(conhecidos, config=None, max_baixas=20, *, ao_receber=None):
    """Lote incremental: históricos conhecidos não ocupam o limite de download.

    ``conhecidos`` contém tuplas (nome, tamanho, mtime) conferidas recentemente.
    O chamador deve expirar esse cache e revalidar conteúdo periodicamente.
    Retorna (arquivos, há_mais_pendentes); todos os acessos continuam só leitura.
    ``ao_receber``, se fornecido, deve guardar cada arquivo completo antes que
    seu handle seja fechado normalmente. Uma falha interrompe o restante do lote.
    """
    __tracebackhide__ = True
    if type(max_baixas) is not int or not 1 <= max_baixas <= 100:
        raise ErroConfiguracaoFiservSFTP(invalidos=('max_baixas',))
    if ao_receber is not None and not callable(ao_receber):
        raise ErroConfiguracaoFiservSFTP(invalidos=('ao_receber',))
    config = config or ConfigFiservSFTP.from_env()
    with _conectar(config) as (sftp, prazo):
        pasta = _pasta_disponivel(sftp, config, prazo)
        pendentes = [item for item in _listar(sftp, pasta, config, prazo)
                     if (item.nome, item.tamanho, item.modificado_em) not in conhecidos]
        lote = []
        tamanho = 0
        for item in pendentes:
            if len(lote) == max_baixas or (lote and tamanho + item.tamanho > config.max_bytes_total):
                break
            if item.tamanho > config.max_bytes_total:
                raise ErroLimiteFiservSFTP('Arquivo Fiserv excede o limite total de bytes por coleta.')
            lote.append(item)
            tamanho += item.tamanho
        arquivos = [_baixar(sftp, pasta, item.nome, config, prazo, esperado=item,
                           ao_receber=ao_receber) for item in lote]
        return arquivos, len(pendentes) > len(lote)

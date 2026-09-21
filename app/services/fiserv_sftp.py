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

import hashlib
import logging
import os
import posixpath
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
PASTA_REMOTA = 'Available'
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


class ErroConexaoFiservSFTP(ErroFiservSFTP):
    pass


class ErroAutenticacaoFiservSFTP(ErroConexaoFiservSFTP):
    """Credenciais recusadas: requer revisão, sem retentativa automática de rede."""


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


@contextmanager
def _conectar(config):
    __tracebackhide__ = True
    paramiko = _carregar_paramiko()
    cliente = None
    sftp = None
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
        autenticacao = (
            {'pkey': config.chave_privada} if config.chave_privada is not None
            else {'key_filename': config.chave_privada_path}
        )
        if config.senha is not None:
            autenticacao['password'] = config.senha
        cliente.connect(
            hostname=config.host, port=PORTA, username=config.usuario,
            passphrase=frase, **autenticacao,
            allow_agent=False, look_for_keys=False,
            timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
            channel_timeout=timeout,
        )
        # O timer pode ter vencido durante DNS/chave, antes de haver transporte
        # para fechar; nesse caso não abre SFTP com um timer já consumido.
        verificar_prazo()
        sftp = cliente.open_sftp()
        verificar_prazo()
        _timeout(sftp, config, prazo)
        yield sftp, prazo
        verificar_prazo()
    except ErroFiservSFTP:
        raise
    except (OSError, ValueError, EOFError, paramiko.SSHException, paramiko.SFTPError) as exc:
        if prazo_expirado.is_set():
            raise ErroLimiteFiservSFTP(_MENSAGEM_PRAZO) from None
        if isinstance(exc, paramiko.BadHostKeyException):
            raise ErroSegurancaFiservSFTP('A chave do servidor Fiserv não corresponde à chave confiável.') from None
        if isinstance(exc, paramiko.AuthenticationException):
            raise ErroAutenticacaoFiservSFTP('A autenticação SFTP Fiserv foi recusada; revise as credenciais.') from None
        raise ErroConexaoFiservSFTP('Não foi possível concluir a leitura SFTP Fiserv.') from None
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
            raise ErroConexaoFiservSFTP('Não foi possível encerrar a conexão SFTP Fiserv.') from None


def _nome_seguro(nome):
    if (
        not isinstance(nome, str) or not nome or len(nome) > 255
        or nome in ('.', '..') or any(c in nome for c in '/\\:')
        or any(ord(c) < 32 or ord(c) == 127 for c in nome)
    ):
        raise ErroSegurancaFiservSFTP('Nome de arquivo remoto inválido.')
    return nome


def _pasta_disponivel(sftp, config, prazo):
    _timeout(sftp, config, prazo)
    atributos = sftp.lstat(PASTA_REMOTA)
    if not isinstance(atributos.st_mode, int) or not stat.S_ISDIR(atributos.st_mode):
        raise ErroSegurancaFiservSFTP('Available precisa ser um diretório real, sem link simbólico.')
    _timeout(sftp, config, prazo)
    home = sftp.normalize('.')
    if not isinstance(home, str) or not home.startswith('/') or posixpath.normpath(home) != home:
        raise ErroSegurancaFiservSFTP('Diretório remoto Fiserv inválido.')
    esperado = posixpath.join(home, PASTA_REMOTA)
    _timeout(sftp, config, prazo)
    if sftp.normalize(PASTA_REMOTA) != esperado:
        raise ErroSegurancaFiservSFTP('Available não pode redirecionar para outro diretório.')
    return esperado


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
    arquivos = []
    nomes = set()
    _timeout(sftp, config, prazo)
    entradas = sftp.listdir_iter(pasta, read_aheads=1)
    try:
        for indice, atributos in enumerate(entradas):
            _timeout(sftp, config, prazo)
            if indice >= config.max_arquivos:
                raise ErroLimiteFiservSFTP('Available excede o limite de entradas por coleta.')
            nome = _nome_seguro(atributos.filename)
            if nome in nomes:
                raise ErroSegurancaFiservSFTP('A listagem Fiserv contém nomes repetidos.')
            nomes.add(nome)
            if isinstance(atributos.st_mode, int) and stat.S_ISDIR(atributos.st_mode):
                continue  # Não percorre subdiretórios; eles também contam para o limite.
            arquivos.append(_metadados(nome, atributos, config))
    finally:
        entradas.close()
    _prazo_restante(config, prazo)
    return sorted(arquivos, key=lambda arquivo: arquivo.nome)


def _verificar_arquivo(sftp, caminho, nome, config, prazo):
    _timeout(sftp, config, prazo)
    metadados = _metadados(nome, sftp.lstat(caminho), config)
    _timeout(sftp, config, prazo)
    if sftp.normalize(caminho) != caminho:
        raise ErroSegurancaFiservSFTP('Arquivo remoto não pode redirecionar para outro caminho.')
    return metadados


def _baixar(sftp, pasta, nome, config, prazo, esperado=None):
    __tracebackhide__ = True  # Não enviar conteúdo bancário em eventos de exceção.
    caminho = posixpath.join(pasta, _nome_seguro(nome))
    antes = _verificar_arquivo(sftp, caminho, nome, config, prazo)
    if antes.tamanho > config.max_bytes_total:
        raise ErroLimiteFiservSFTP('Arquivo Fiserv excede o limite total de bytes por coleta.')
    if esperado is not None and antes != esperado:
        raise ErroSegurancaFiservSFTP('Arquivo Fiserv mudou após a listagem; tente novamente.')
    conteudo = bytearray()
    _timeout(sftp, config, prazo)
    with sftp.open(caminho, 'rb', bufsize=0) as remoto:
        _timeout(sftp, config, prazo)
        aberto = _metadados(nome, remoto.stat(), config)
        if aberto != antes or _verificar_arquivo(sftp, caminho, nome, config, prazo) != antes:
            raise ErroSegurancaFiservSFTP('Arquivo Fiserv mudou antes da leitura; tente novamente.')
        while True:
            _timeout(sftp, config, prazo)
            bloco = remoto.read(min(_BLOCO, antes.tamanho - len(conteudo) + 1))
            if not bloco:
                break
            conteudo.extend(bloco)
            if len(conteudo) > antes.tamanho or len(conteudo) > config.max_bytes_arquivo:
                raise ErroLimiteFiservSFTP('Arquivo Fiserv excedeu o tamanho informado durante a leitura.')
        _timeout(sftp, config, prazo)
        depois = _metadados(nome, remoto.stat(), config)
    # SFTP v3 não oferece OPEN_NOFOLLOW: as verificações antes/depois recusam
    # links e trocas observáveis, mas não autenticam o conteúdo de um servidor
    # comprometido. A confiança no host provisionado continua obrigatória.
    final = _verificar_arquivo(sftp, caminho, nome, config, prazo)
    if len(conteudo) != antes.tamanho or depois != antes or final != antes:
        raise ErroSegurancaFiservSFTP('Arquivo Fiserv mudou durante a leitura; tente novamente.')
    dados = bytes(conteudo)
    return ArquivoFiserv(metadados=antes, conteudo=dados, sha256=hashlib.sha256(dados).hexdigest())


def listar_arquivos(config: ConfigFiservSFTP | None = None) -> list[MetadadosArquivoFiserv]:
    """Lista arquivos regulares diretamente em Available, sem baixar conteúdo."""
    __tracebackhide__ = True
    config = config or ConfigFiservSFTP.from_env()
    with _conectar(config) as (sftp, prazo):
        return _listar(sftp, _pasta_disponivel(sftp, config, prazo), config, prazo)


def baixar_arquivo(nome: str, config: ConfigFiservSFTP | None = None) -> ArquivoFiserv:
    """Baixa um nome simples de Available para memória; não grava nem remove."""
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


def coletar_lote_pendente(conhecidos, config=None, max_baixas=20):
    """Lote incremental: históricos conhecidos não ocupam o limite de download.

    ``conhecidos`` contém tuplas (nome, tamanho, mtime) conferidas recentemente.
    O chamador deve expirar esse cache e revalidar conteúdo periodicamente.
    Retorna (arquivos, há_mais_pendentes); todos os acessos continuam só leitura.
    """
    __tracebackhide__ = True
    if type(max_baixas) is not int or not 1 <= max_baixas <= 100:
        raise ErroConfiguracaoFiservSFTP(invalidos=('max_baixas',))
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
        arquivos = [_baixar(sftp, pasta, item.nome, config, prazo, esperado=item) for item in lote]
        return arquivos, len(pendentes) > len(lote)

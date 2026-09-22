"""Coleta EDI cifrada e idempotente. Não altera pagamentos nem saldos."""

import base64
import hashlib
import logging
import os
import socket
import threading
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from flask import current_app
from sqlalchemy import text

from app.extensions import db
from app.models.fiserv import ArquivoFiservRecebido, EventoFiserv, FiservArquivoRemoto, IntegracaoFiserv
from app.services.fiserv_segredos import (
    ErroSegredoFiserv,
    carregar_chave,
    cifrar_bytes,
    decifrar_json,
)
from app.services.fiserv_sftp import (
    HOSTS_PERMITIDOS,
    PORTA,
    ConfigFiservSFTP,
    ErroAcessoFiservSFTP,
    ErroAutenticacaoFiservSFTP,
    ErroConexaoFiservSFTP,
    ErroEtapaAutenticacaoFiservSFTP,
    ErroFiservSFTP,
    ErroOperacaoFiservSFTP,
    ErroPersistenciaFiservSFTP,
    baixar_arquivo,
    coletar_lote_pendente,
    listar_arquivos,
)
from app.services.instancia import BRANCH_PRODUCAO
from app.utils import agora

_LOCK_ID = 718420621
_MENSAGEM_ARMAZENAMENTO = 'Não foi possível armazenar a coleta. Solicite uma nova tentativa.'
logger = logging.getLogger(__name__)


class ColetaEmAndamento(ValueError):
    pass


def instancia_autorizada():
    branch = os.environ.get('RAILWAY_GIT_BRANCH', '')
    if branch:
        return branch == BRANCH_PRODUCAO
    return os.environ.get('FISERV_INSTANCIA_LOCAL_AUTORIZADA') == '1'


def coleta_disponivel():
    return (instancia_autorizada() and os.environ.get('SERU_AUTO_SYNC', '1') != '0'
            and os.environ.get('FISERV_AUTO_COLETA', '1') != '0')


@contextmanager
def trava():
    """Um ciclo/configuração por vez, entre workers e também no Windows."""
    if db.engine.dialect.name == 'postgresql':
        with db.engine.connect() as conn:
            if not conn.execute(text('SELECT pg_try_advisory_lock(:k)'), {'k': _LOCK_ID}).scalar():
                raise ColetaEmAndamento('Uma coleta está em andamento. Aguarde e tente novamente.')
            try:
                yield
            finally:
                try:
                    conn.execute(text('SELECT pg_advisory_unlock(:k)'), {'k': _LOCK_ID})
                except Exception:
                    conn.invalidate()
                    raise
        return
    caminho = Path(current_app.instance_path) / 'fiserv-coleta.lock'
    caminho.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(caminho, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b'0')
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ColetaEmAndamento('Uma coleta está em andamento. Aguarde e tente novamente.') from None
        yield
    finally:
        os.close(fd)


def obter_chave_apresentada(host):
    """Handshake SEM credenciais; observar chave não comprova sua identidade."""
    __tracebackhide__ = True
    if host not in HOSTS_PERMITIDOS:
        raise ValueError('Servidor Fiserv inválido.')
    import paramiko
    transporte = None
    temporizador = None
    canal_log = 'app.services.fiserv_sftp.ssh'
    log = logging.getLogger(canal_log)
    log.setLevel(logging.CRITICAL + 1)
    log.propagate = False
    try:
        with socket.create_connection((host, PORTA), timeout=10) as sock:
            transporte = paramiko.Transport(sock)
            transporte.set_log_channel(canal_log)
            transporte.banner_timeout = 10
            temporizador = threading.Timer(15, transporte.close)
            temporizador.daemon = True
            temporizador.start()
            transporte.start_client(timeout=10)
            chave = transporte.get_remote_server_key()
            fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(chave.asbytes()).digest()).decode().rstrip('=')
            linha = f'[{host}]:{PORTA} {chave.get_name()} {chave.get_base64()}'
            return {'host': host, 'fingerprint': fingerprint, 'known_hosts_text': linha}
    except (OSError, EOFError, ValueError, paramiko.SSHException):
        raise ErroConexaoFiservSFTP('Não foi possível consultar o servidor Fiserv. Tente novamente.') from None
    finally:
        if temporizador:
            temporizador.cancel()
            temporizador.join()
        if transporte:
            transporte.close()


def _config_coleta(registro, *, duracao_maxima_segundos=120):
    __tracebackhide__ = True
    dados = decifrar_json(registro.acesso_cifrado)
    chave = carregar_chave(base64.b64decode(dados['chave_base64'], validate=True), dados.get('passphrase'))
    return ConfigFiservSFTP(
        usuario=dados['usuario'], host=dados['host'], chave_privada=chave,
        senha=dados.get('senha'), known_hosts_text=dados['known_hosts_text'],
        max_arquivos=10000, max_bytes_arquivo=16 * 1024 * 1024,
        max_bytes_total=64 * 1024 * 1024, duracao_maxima_segundos=duracao_maxima_segundos,
    )


def _guardar_arquivo(registro, arquivo):
    """Guarda por arquivo antes do CLOSE; retorna (id persistido, novo)."""
    __tracebackhide__ = True
    remoto = FiservArquivoRemoto.query.filter_by(
        nome=arquivo.metadados.nome, versao_configuracao=registro.versao,
    ).first()
    if remoto is None:
        remoto = FiservArquivoRemoto(nome=arquivo.metadados.nome,
                                    versao_configuracao=registro.versao)
        db.session.add(remoto)
    remoto.tamanho = arquivo.metadados.tamanho
    remoto.modificado_remoto = arquivo.metadados.modificado_em
    remoto.sha256 = arquivo.sha256
    remoto.conferido_em = agora()
    with db.session.no_autoflush:
        recebido = ArquivoFiservRecebido.query.filter_by(sha256=arquivo.sha256).first()
    novo = recebido is None
    if novo:
        recebido = ArquivoFiservRecebido(
            nome=arquivo.metadados.nome, sha256=arquivo.sha256,
            tamanho=arquivo.metadados.tamanho,
            modificado_remoto=arquivo.metadados.modificado_em,
            conteudo_cifrado=cifrar_bytes(arquivo.conteudo),
            versao_configuracao=registro.versao,
        )
        db.session.add(recebido)
    db.session.commit()
    return recebido.id, novo


def _registro_consulta():
    __tracebackhide__ = True
    if not coleta_disponivel():
        raise ErroFiservSFTP('A consulta não está disponível neste ambiente.')
    registro = db.session.get(IntegracaoFiserv, 1)
    if registro is None:
        raise ErroFiservSFTP('Configure o acesso antes de consultar os arquivos.')
    if registro.ativa or registro.coleta_solicitada:
        raise ErroFiservSFTP('Pause a coleta automática antes de conferir um arquivo específico.')
    return registro


def consultar_arquivos_disponiveis():
    """Consulta isolada do owner, sem ativar nem solicitar coleta automática."""
    __tracebackhide__ = True
    with trava():
        registro = _registro_consulta()
        arquivos = listar_arquivos(_config_coleta(registro, duracao_maxima_segundos=45))
        return registro.versao, arquivos


def receber_arquivo_selecionado(esperado, versao, usuario_id):
    """Recebe só a seleção validada no POST, mantendo o automático pausado."""
    __tracebackhide__ = True
    with trava():
        registro = _registro_consulta()
        if registro.versao != versao:
            raise ErroFiservSFTP('O acesso foi alterado. Consulte a lista de arquivos novamente.')
        arquivo_id = None
        novo = False

        def receber(arquivo):
            __tracebackhide__ = True
            nonlocal arquivo_id, novo
            arquivo_id, novo = _guardar_arquivo(registro, arquivo)

        baixar_arquivo(esperado.nome, _config_coleta(registro, duracao_maxima_segundos=45),
                       esperado=esperado, ao_receber=receber)
        if arquivo_id is None:
            raise ErroPersistenciaFiservSFTP()
        db.session.add(EventoFiserv(acao='arquivo_recebido_manual', usuario_id=usuario_id,
                                   arquivos_novos=int(novo)))
        db.session.commit()
        return arquivo_id


def _registrar_falha(registro_id, pausar, mensagem):
    __tracebackhide__ = True
    db.session.rollback()
    registro = db.session.get(IntegracaoFiserv, registro_id)
    if registro is not None:
        registro.estado = 'erro'
        registro.mensagem = mensagem
        registro.coleta_solicitada = False
        registro.ativa = not pausar
        db.session.add(EventoFiserv(acao='coleta_falhou'))
        db.session.commit()


def executar_ciclo():
    __tracebackhide__ = True
    if not coleta_disponivel():
        return 0
    try:
        with trava():
            registro = db.session.get(IntegracaoFiserv, 1)
            if not registro or not (registro.ativa or registro.coleta_solicitada):
                return 0
            if (not registro.coleta_solicitada and registro.ultima_tentativa_em
                    and registro.ultima_tentativa_em > agora() - timedelta(hours=1)):
                return 0
            registro.ultima_tentativa_em = agora()
            registro.estado = 'coletando'
            db.session.commit()
            try:
                conferidos = FiservArquivoRemoto.query.filter(
                    FiservArquivoRemoto.versao_configuracao == registro.versao,
                    FiservArquivoRemoto.conferido_em > agora() - timedelta(hours=24),
                ).all()
                conhecidos = {(item.nome, item.tamanho, item.modificado_remoto) for item in conferidos}
                novos = 0

                def receber(arquivo):
                    __tracebackhide__ = True
                    nonlocal novos
                    _, novo = _guardar_arquivo(registro, arquivo)
                    if novo:
                        novos += 1

                _, mais_pendentes = coletar_lote_pendente(
                    conhecidos, _config_coleta(registro), ao_receber=receber,
                )
                registro.ativa = True
                registro.coleta_solicitada = mais_pendentes
                registro.estado = 'conectado'
                registro.ultimo_sucesso_em = agora()
                registro.mensagem = ('Lote recebido. Os arquivos restantes serão coletados automaticamente.'
                                     if mais_pendentes else 'Coleta concluída.')
                db.session.add(EventoFiserv(acao='coleta_concluida', arquivos_novos=novos))
                db.session.commit()
                return novos
            except ErroEtapaAutenticacaoFiservSFTP as exc:
                # Somente etapa e motivo locais; não inclui resposta do servidor.
                _registrar_falha(1, True, str(exc))
                return 0
            except ErroAutenticacaoFiservSFTP:
                _registrar_falha(1, True, 'A Fiserv recusou o acesso. Confira as credenciais antes de solicitar nova coleta.')
                return 0
            except ErroAcessoFiservSFTP as exc:
                # A classe aceita somente códigos e mensagens fixos; nunca a
                # resposta privada do servidor ou os dados do acesso.
                _registrar_falha(1, True, str(exc))
                return 0
            except ErroOperacaoFiservSFTP as exc:
                # Etapa e motivo são allowlists locais, sem texto remoto.
                _registrar_falha(1, False, str(exc))
                return 0
            except ErroConexaoFiservSFTP:
                _registrar_falha(1, False, 'Conexão indisponível no momento. Nova tentativa automática em uma hora.')
                return 0
            except ErroPersistenciaFiservSFTP:
                _registrar_falha(1, True, _MENSAGEM_ARMAZENAMENTO)
                logger.error('Falha no armazenamento Fiserv; detalhes privados omitidos.')
                return 0
            except (ErroFiservSFTP, ErroSegredoFiserv, KeyError, ValueError):
                # Credenciais/host incorretos não são repetidos automaticamente:
                # evita tentativas sucessivas que bloqueariam a conta na Fiserv.
                _registrar_falha(1, True, 'Coleta interrompida. Confira o acesso e a identificação do servidor antes de tentar novamente.')
                return 0
            except Exception:
                _registrar_falha(1, True, _MENSAGEM_ARMAZENAMENTO)
                logger.error('Falha no armazenamento Fiserv; detalhes privados omitidos.')
                return 0
    except ColetaEmAndamento:
        return 0


def rodar_agendado(app):
    __tracebackhide__ = True
    import sentry_sdk
    with app.app_context(), sentry_sdk.isolation_scope() as scope:
        scope.set_tag('fiserv_privado', True)
        try:
            executar_ciclo()
        except Exception:
            db.session.rollback()
            logger.error('Ciclo Fiserv não concluído; confira o painel privado.')
        finally:
            db.session.remove()

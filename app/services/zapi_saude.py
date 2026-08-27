"""Saúde da instância Z-API: webhooks, polling e alertas persistentes."""
import hashlib
import hmac
import logging
from datetime import timedelta

from flask import current_app

from app.extensions import db
from app.utils import agora

logger = logging.getLogger(__name__)

_KEY_ESTADO = 'zapi_conexao_estado'
_KEY_ERRO = 'zapi_conexao_erro'
_KEY_MUDOU_EM = 'zapi_conexao_mudou_em'
_KEY_WEBHOOKS_EM = 'zapi_webhooks_assinados_em'


def webhook_token():
    """Token estável derivado do SECRET_KEY; não exige mais uma variável."""
    segredo = str(current_app.config.get('SECRET_KEY') or '').encode()
    instancia = str(
        current_app.config.get('ZAPI_INSTANCE_ID') or '').encode()
    return hmac.new(segredo, b'zapi-webhook:' + instancia,
                    hashlib.sha256).hexdigest()


def token_valido(recebido):
    esperado = webhook_token()
    return bool(recebido and esperado
                and hmac.compare_digest(str(recebido), esperado))


def _instancia_valida(payload):
    esperada = str(current_app.config.get('ZAPI_INSTANCE_ID') or '').strip()
    recebida = str((payload or {}).get('instanceId') or '').strip()
    return bool(esperada and recebida and hmac.compare_digest(
        recebida, esperada))


def _gravar_estado(estado, detalhe):
    from app.models import AppConfig
    AppConfig.set(_KEY_ESTADO, estado)
    AppConfig.set(_KEY_ERRO, detalhe or '')
    AppConfig.set(_KEY_MUDOU_EM, agora().isoformat())
    db.session.commit()


def _emails_alerta():
    from app.models import Usuario
    emails = {
        (u.email or '').strip().lower()
        for u in Usuario.query.filter_by(is_owner=True).all()
        if '@' in (u.email or '')
    }
    fallback = (current_app.config.get('EMAIL_REPLY_TO')
                or 'contato@opao.online').strip().lower()
    if not emails and '@' in fallback:
        emails.add(fallback)
    return sorted(emails)


def _alertar_email(evento, detalhe):
    """Alerta owner por Postmark. Best-effort; o Sentry segue independente."""
    from app.services import email
    caiu = evento == 'desconectado'
    assunto = ('🚨 WhatsApp do ERP desconectado' if caiu
               else '✅ WhatsApp do ERP reconectado')
    base = (current_app.config.get('APP_BASE_URL') or '').rstrip('/')
    acao = (f'<p><a href="{base}/notificacoes/qr">Abrir reconexão por QR</a></p>'
            if caiu and base else '')
    html = (
        f'<h2>{assunto}</h2><p>{detalhe or "Sem detalhe informado."}</p>'
        f'{acao}<p>Instante: {agora().strftime("%d/%m/%Y %H:%M")}</p>')
    texto = f'{assunto}\n{detalhe or "Sem detalhe informado."}'
    for destino in _emails_alerta():
        resultado = email.enviar(destino, assunto, html, texto=texto)
        if not resultado.get('ok'):
            logger.error('zapi_saude: email de alerta falhou: %s',
                         resultado.get('erro'))


def _alertar_sentry(evento, detalhe):
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            scope.set_tag('integracao', 'zapi')
            scope.set_tag('estado_zapi', evento)
            scope.set_extra('detalhe', detalhe)
            sentry_sdk.capture_message(
                f'Z-API {evento}: {detalhe}',
                level='error' if evento == 'desconectado' else 'info')
    except Exception:  # noqa: BLE001 — alerta nunca derruba o webhook
        logger.exception('zapi_saude: alerta Sentry falhou')


def registrar_desconexao(payload):
    """Persiste a queda e alerta apenas na transição conectado→desconectado."""
    if not _instancia_valida(payload):
        return {'ok': False, 'erro': 'instanceId invalido'}
    from app.models import AppConfig
    anterior = AppConfig.get(_KEY_ESTADO)
    detalhe = str((payload or {}).get('error') or 'Instância desconectada')[:300]
    mudou = anterior != 'desconectado'
    if mudou:
        _gravar_estado('desconectado', detalhe)
        _alertar_email('desconectado', detalhe)
        _alertar_sentry('desconectado', detalhe)
    elif detalhe != AppConfig.get(_KEY_ERRO):
        AppConfig.set(_KEY_ERRO, detalhe)
        db.session.commit()
    return {'ok': True, 'estado': 'desconectado', 'alertou': mudou}


def registrar_conexao(payload):
    """Fecha o estado de queda e comunica a normalização uma única vez."""
    if not _instancia_valida(payload):
        return {'ok': False, 'erro': 'instanceId invalido'}
    from app.models import AppConfig
    anterior = AppConfig.get(_KEY_ESTADO)
    normalizou = anterior == 'desconectado'
    if anterior != 'conectado':
        detalhe = 'Instância conectada'
        _gravar_estado('conectado', detalhe)
        if normalizou:
            _alertar_email('conectado', detalhe)
            _alertar_sentry('conectado', detalhe)
    return {'ok': True, 'estado': 'conectado', 'normalizou': normalizou}


def garantir_assinatura(*, force=False):
    """Registra callbacks na Z-API, no máximo uma vez por dia."""
    from app.models import AppConfig
    ultima = AppConfig.get(_KEY_WEBHOOKS_EM)
    if ultima and not force:
        try:
            from datetime import datetime
            if agora() - datetime.fromisoformat(ultima) < timedelta(days=1):
                return {'ok': True, 'ja_assinado': True}
        except (TypeError, ValueError):
            pass
    from app.services import zapi
    resultado = zapi.assinar_webhooks_conexao(
        current_app.config.get('APP_BASE_URL'), webhook_token())
    if resultado.get('ok'):
        AppConfig.set(_KEY_WEBHOOKS_EM, agora().isoformat())
        db.session.commit()
    return resultado


def monitorar_conexao():
    """Polling de segurança; garante alerta em até um ciclo de cinco minutos."""
    from app.services import instancia
    if not instancia.pode_falar_com_o_mundo('zapi'):
        return {'ok': True, 'suprimido_instancia': True}
    try:
        garantir_assinatura()
    except Exception:  # noqa: BLE001 — polling de status ainda precisa rodar
        db.session.rollback()
        logger.exception('zapi_saude: assinatura automática falhou')
    from app.services import zapi
    status = zapi.status_instancia()
    instancia = current_app.config.get('ZAPI_INSTANCE_ID')
    if status.get('ok') and status.get('conectado'):
        return registrar_conexao({
            'instanceId': instancia, 'connected': True,
        })
    detalhe = status.get('detalhe') or 'Não foi possível consultar /status'
    return registrar_desconexao({
        'instanceId': instancia, 'disconnected': True, 'error': detalhe,
    })

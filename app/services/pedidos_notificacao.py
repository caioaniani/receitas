"""Notificacao de pedido recebido na loja (WhatsApp do dono).

Quando um pedido vira `entregue` (= recebido na loja), manda 1 mensagem pra
`ZAPI_BOT_DONO_NUMERO` com o link compartilhado da pasta de fotos no
Dropbox (`/recebimento/<pedido_id>/`).

Idempotente: marca `pedido.observacao` com um sentinela apos enviar pra
nunca avisar o mesmo pedido duas vezes (acontece quando o copilot recebe
um pedido ja entregue, ou quando o admin forca entrega em cima de status
ja entregue).

Best-effort: falha do aviso (Z-API caiu, Dropbox sem link, etc) NUNCA
bloqueia a entrega. So loga.
"""
import logging

from flask import current_app

logger = logging.getLogger(__name__)

# String invisivel posta em pedido.observacao depois do envio. Curta e
# distintiva pra nunca colidir com texto humano de observacao.
_SENTINELA = '[avisado-fotos]'


def _disponivel():
    cfg = current_app.config
    if not cfg.get('ZAPI_BOT_AVISO_RECEBIMENTO', True):
        return False
    return bool((cfg.get('ZAPI_BOT_DONO_NUMERO') or '').strip())


def _ja_avisado(pedido):
    obs = pedido.observacao or ''
    return _SENTINELA in obs


def _marcar_avisado(pedido):
    from app.extensions import db
    obs = (pedido.observacao or '').rstrip()
    sep = ' ' if obs else ''
    pedido.observacao = f'{obs}{sep}{_SENTINELA}'
    try:
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        logger.exception('falha marcando sentinela de aviso no pedido %s',
                          pedido.id)


def notificar_pedido_recebido(pedido):
    """Dispara o aviso pro WhatsApp do dono. Idempotente, best-effort.

    Chame DEPOIS de `pedido.status = 'entregue'` (e do commit, idealmente).
    Sem fotos ou sem Dropbox configurado, ainda avisa com o resumo do
    pedido — so omite o link."""
    if not pedido or pedido.status != 'entregue':
        return
    if _ja_avisado(pedido):
        return
    if not _disponivel():
        return

    try:
        from app.services import dropbox_storage, zapi

        # Tenta o link da pasta. Se nao tiver foto / Dropbox nao
        # configurado / API caiu, segue sem link.
        link_pasta = None
        try:
            link_pasta = dropbox_storage.shared_link_pasta(
                f'/recebimento/{pedido.id}')
        except Exception:  # noqa: BLE001
            logger.exception('shared link da pasta de fotos falhou pedido=%s',
                              pedido.id)

        n_fotos = 0
        try:
            n_fotos = len(pedido.fotos or [])
        except Exception:  # noqa: BLE001
            pass

        loja = getattr(pedido.loja, 'nome', '?') if pedido.loja else '?'
        linhas = [
            '📦 *Pedido recebido na loja*',
            f'Pedido #{pedido.id} · {loja}',
        ]
        if n_fotos:
            linhas.append(f'{n_fotos} foto(s) de conferencia')
        if link_pasta:
            linhas.append('')
            linhas.append(link_pasta)
        elif n_fotos == 0:
            linhas.append('_(sem fotos)_')
        else:
            linhas.append('_(link da pasta indisponivel — '
                          'veja em /pedidos/' + str(pedido.id) + ')_')

        numero = current_app.config.get('ZAPI_BOT_DONO_NUMERO') or ''
        if not numero:
            return
        resp = zapi.enviar_texto(numero, '\n'.join(linhas))
        if resp.get('ok'):
            _marcar_avisado(pedido)
        else:
            logger.warning('aviso pedido recebido falhou pedido=%s: %s',
                           pedido.id, resp.get('erro'))
    except Exception:  # noqa: BLE001
        logger.exception('notificar_pedido_recebido falhou pedido=%s',
                          pedido.id)

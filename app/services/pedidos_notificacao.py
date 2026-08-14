"""Notificacao de pedidos recebidos na loja (WhatsApp do dono).

DIGEST DIARIO desde 14/08/2026 (dono: "os pedidos recebidos pelas lojas
podem ser acumulados ate as 12:00 dai dispara uma unica mensagem ao inves
de mandar picado — esta ficando meio flodado"): os pedidos que viram
`entregue` acumulam e o cron das 12:00 BRT (`seru_cron`, lock 7760) manda
UMA mensagem com todos, cada um com o link da pasta de fotos no Dropbox.
Pedido recebido DEPOIS das 12:00 entra no digest do dia seguinte.

O aviso IMEDIATO por pedido (`notificar_pedido_recebido`) continua
existindo so pra rota de teste do owner (/admin/teste-aviso-recebimento)
— os caminhos de entrega reais NAO chamam mais.

Idempotente: marca `pedido.observacao` com um sentinela apos enviar pra
nunca avisar o mesmo pedido duas vezes. O digest acha os pendentes por
status='entregue' + AUSENCIA do sentinela (janela de _JANELA_DIAS por
`data_entrega`, com fallback pra data de criacao quando NULL) — envio que
falhar fica sem sentinela e re-entra no digest seguinte.

Best-effort: falha do aviso (Z-API caiu, Dropbox sem link, etc) NUNCA
bloqueia a entrega. So loga.
"""
import logging
from datetime import timedelta

from flask import current_app

from app.utils import hoje

logger = logging.getLogger(__name__)

# String invisivel posta em pedido.observacao depois do envio. Curta e
# distintiva pra nunca colidir com texto humano de observacao.
_SENTINELA = '[avisado-fotos]'

# Quantos dias pra tras o digest procura entregues sem aviso. Cobre fim de
# semana com Z-API fora (retentativa via ausencia de sentinela) sem nunca
# ressuscitar pedido antigo de antes do regime.
_JANELA_DIAS = 3

# Cap de pedidos por digest (padrao da casa: mensagem gigante vira ruido).
# Os que passarem do cap NAO sao marcados — saem no digest seguinte.
_MAX_PEDIDOS_DIGEST = 20

# Marcador que a rota de teste do owner poe na observacao do pedido
# sintetico (/admin/teste-aviso-recebimento). O digest PULA esses pedidos:
# se o envio imediato do teste falhar (que e justamente o cenario de quem
# esta debugando o pipe), o pedido fake nao pode vazar pro digest real.
_MARCA_TESTE = '[PEDIDO-TESTE-AVISO]'


def _disponivel():
    cfg = current_app.config
    if not cfg.get('ZAPI_BOT_AVISO_RECEBIMENTO', True):
        return False
    return bool((cfg.get('ZAPI_BOT_DONO_NUMERO') or '').strip())


def _ja_avisado(pedido):
    obs = pedido.observacao or ''
    return _SENTINELA in obs


def _marcar_avisado(pedido, commit=True):
    from app.extensions import db
    obs = (pedido.observacao or '').rstrip()
    sep = ' ' if obs else ''
    pedido.observacao = f'{obs}{sep}{_SENTINELA}'
    if not commit:
        return
    try:
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
        logger.exception('falha marcando sentinela de aviso no pedido %s',
                          pedido.id)


def _fotos_e_link(pedido):
    """Conta as fotos do pedido e resolve o shared link da pasta no
    Dropbox. Devolve (n_fotos, link_ou_None). Nunca levanta.

    As fotos REAIS do pedido sao as da CONFERENCIA do handshake
    (PedidoItemFoto, em /conferencia/<id>/) — o motorista fotografa cada
    item. FotoRecebimento (/recebimento/<id>/) so existe no recebimento
    manual com upload. Bug original (2026-06-10): so olhavamos
    /recebimento e o aviso dizia "(sem fotos)" com a pasta de conferencia
    cheia. A pasta e DERIVADA do storage_path gravado em cada foto."""
    from app.services import dropbox_storage

    paths = []
    try:
        from app.models import PedidoItem, PedidoItemFoto
        fotos_conf = (PedidoItemFoto.query
                      .join(PedidoItem,
                            PedidoItemFoto.pedido_item_id == PedidoItem.id)
                      .filter(PedidoItem.pedido_id == pedido.id)
                      .all())
        paths += [f.imagem_storage_path for f in fotos_conf
                  if f.imagem_storage_path]
    except Exception:  # noqa: BLE001
        logger.exception('contagem de fotos de conferencia falhou pedido=%s',
                          pedido.id)
    try:
        paths += [f.imagem_storage_path for f in (pedido.fotos or [])
                  if f.imagem_storage_path]
    except Exception:  # noqa: BLE001
        pass

    link_pasta = None
    if paths:
        pasta = paths[0].rsplit('/', 1)[0]
        try:
            link_pasta = dropbox_storage.shared_link_pasta(pasta)
        except Exception:  # noqa: BLE001
            logger.exception('shared link da pasta %s falhou pedido=%s',
                              pasta, pedido.id)
    return len(paths), link_pasta


def _linhas_pedido(pedido):
    """Bloco de texto de UM pedido (usado no aviso imediato e no digest)."""
    loja = getattr(pedido.loja, 'nome', '?') if pedido.loja else '?'
    n_fotos, link_pasta = _fotos_e_link(pedido)
    linhas = [f'Pedido #{pedido.id} · {loja}']
    if n_fotos:
        linhas.append(f'{n_fotos} foto(s) de conferencia')
    if link_pasta:
        linhas.append(link_pasta)
    elif n_fotos == 0:
        linhas.append('_(sem fotos)_')
    else:
        linhas.append('_(link da pasta indisponivel — '
                      'veja em /pedidos/' + str(pedido.id) + ')_')
    return linhas


def pedidos_pendentes_de_aviso():
    """Pedidos entregues recentes ainda sem o sentinela de aviso.

    Janela por QUALQUER um dos tres marcos >= corte: `data_entrega` (data
    planejada), `modificado_em` (carimbado no ato do recebimento pelo
    `_executar_recebimento_pedido` — cobre pedido recebido com ATRASO,
    cuja data planejada ja saiu da janela) e, sem `data_entrega`,
    `criado_em`. O corte existe so pra nao ressuscitar entregues antigos
    de antes do regime de sentinela (pre-06/2026, todos sem marca)."""
    from sqlalchemy import func, or_

    from app.models import PedidoLoja

    corte = hoje() - timedelta(days=_JANELA_DIAS)
    candidatos = (PedidoLoja.query
                  .filter(PedidoLoja.status == 'entregue')
                  .filter(or_(PedidoLoja.data_entrega >= corte,
                              func.date(PedidoLoja.modificado_em) >= corte,
                              PedidoLoja.data_entrega.is_(None)
                              & (func.date(PedidoLoja.criado_em) >= corte)))
                  .order_by(PedidoLoja.id)
                  .all())
    return [p for p in candidatos
            if not _ja_avisado(p)
            and _MARCA_TESTE not in (p.observacao or '')]


def enviar_digest_recebimentos():
    """Job das 12:00 BRT: UMA mensagem com todos os pedidos recebidos
    ainda nao avisados. Sem pendentes = nada enviado. Envio ok marca o
    sentinela de TODOS; falha nao marca nenhum (re-tenta no proximo dia,
    dentro da janela). Devolve dict de status pro log/cron."""
    if not _disponivel():
        return {'enviado': False, 'motivo': 'indisponivel'}
    try:
        pendentes = pedidos_pendentes_de_aviso()
        if not pendentes:
            return {'enviado': False, 'motivo': 'sem_pendentes'}

        from app.services import zapi
        partes = [f'📦 *Pedidos recebidos nas lojas* ({len(pendentes)})']
        for p in pendentes:
            partes.append('')
            partes.extend(_linhas_pedido(p))

        numero = current_app.config.get('ZAPI_BOT_DONO_NUMERO') or ''
        resp = zapi.enviar_texto(numero, '\n'.join(partes))
        if not resp.get('ok'):
            logger.warning('digest recebimentos falhou: %s', resp.get('erro'))
            return {'enviado': False, 'motivo': 'erro_envio',
                    'erro': resp.get('erro'), 'pendentes': len(pendentes)}

        from app.extensions import db
        for p in pendentes:
            _marcar_avisado(p, commit=False)
        try:
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()
            logger.exception('falha marcando sentinelas do digest')
        logger.info('digest recebimentos: %d pedido(s) avisados',
                    len(pendentes))
        return {'enviado': True, 'pedidos': len(pendentes)}
    except Exception:  # noqa: BLE001
        logger.exception('enviar_digest_recebimentos falhou')
        return {'enviado': False, 'motivo': 'excecao'}


def notificar_pedido_recebido(pedido):
    """Aviso IMEDIATO de UM pedido — hoje so a rota de teste do owner usa
    (/admin/teste-aviso-recebimento valida o pipe Z-API+Dropbox de ponta a
    ponta). O fluxo real acumula pro digest das 12:00. Idempotente,
    best-effort."""
    if not pedido or pedido.status != 'entregue':
        return
    if _ja_avisado(pedido):
        return
    if not _disponivel():
        return

    try:
        from app.services import zapi

        linhas = ['📦 *Pedido recebido na loja*'] + _linhas_pedido(pedido)
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

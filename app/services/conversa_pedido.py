"""Vínculo conversa ↔ pedido + aviso à equipe quando o status da entrega
muda (item 8 da spec do dono, caso E3862E49, 22/09/2026).

O pedido E3862E49 gerou três conversas (2429 WhatsApp de saída, 2431
Instagram da compradora, 2432 WhatsApp do marido) e a equipe respondia em
uma sem saber das outras. Agora:

- `vincular(conv_id, pedido_code, origem)` grava o par uma vez, quando o
  bot identifica o pedido (`consultar_pedido` autorizado OU existente sem
  autorização — a nota interna já diz o código), quando o socorro da
  falha operacional localiza, quando a equipe clica "Chamar cliente" e
  quando o alerta da Lalamove abre a conversa. Sessão ISOLADA e
  best-effort: roda dentro do turno do bot e de rotas com transação
  própria, e nunca pode derrubá-los.
- `alertar_mudanca_status(pedido_code, rotulo, detalhe)` (thread): para
  cada conversa vinculada ainda ABERTA (open/pending no Chatwoot), nota
  PRIVADA com o status novo e a lista das outras conversas abertas do
  mesmo pedido. NUNCA mensagem ao cliente (contrato: `enviar_nota_privada`
  é a única fala).
"""
import logging
from concurrent.futures import ThreadPoolExecutor

from flask import current_app

logger = logging.getLogger(__name__)

ORIGENS = ('bot', 'socorro', 'chamar', 'lalamove')
STATUS_ABERTOS = ('open', 'pending')
_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='conversa-pedido')
_ROTULO_STATUS = {'a_caminho': 'A CAMINHO', 'entregue': 'ENTREGUE'}


def _conv_ok(conv_id):
    cid = str(conv_id or '').strip()
    return cid if cid.isdigit() and int(cid) > 0 else ''


def _code(pedido_code):
    return str(pedido_code or '').strip().upper()[:40]


def vincular(conv_id, pedido_code, origem):
    """Grava (conversa, pedido) uma vez. Devolve True se criou agora."""
    cid, code = _conv_ok(conv_id), _code(pedido_code)
    if not cid or not code:
        return False
    from sqlalchemy.orm import Session

    from app.extensions import db
    from app.models import ConversaPedido
    try:
        with Session(db.engine) as s:
            ja = (s.query(ConversaPedido)
                  .filter_by(conv_id=cid, pedido_code=code).first())
            if ja is not None:
                return False
            s.add(ConversaPedido(conv_id=cid, pedido_code=code,
                                 origem=(origem or '')[:20] or None))
            s.commit()
        logger.info('conversa_pedido: conv %s <-> pedido %s (%s)', cid, code, origem)
        return True
    except Exception:  # noqa: BLE001
        logger.exception('conversa_pedido: vincular conv=%s pedido=%s falhou', cid, code)
        return False


def conversas_do_pedido(pedido_code):
    """conv_ids vinculados ao pedido, do mais antigo pro mais novo."""
    from app.models import ConversaPedido
    code = _code(pedido_code)
    if not code:
        return []
    rows = (ConversaPedido.query.filter_by(pedido_code=code)
            .order_by(ConversaPedido.criado_em.asc(), ConversaPedido.id.asc()).all())
    vistos, out = set(), []
    for r in rows:
        if r.conv_id not in vistos:
            vistos.add(r.conv_id)
            out.append(r.conv_id)
    return out


def _rotulo_canal(conv):
    meta = (conv or {}).get('meta') or {}
    canal = str(meta.get('channel') or '').replace('Channel::', '')
    return {'Whatsapp': 'WhatsApp', 'Instagram': 'Instagram', 'FacebookPage': 'Facebook',
            'WebWidget': 'site', 'Api': 'API'}.get(canal, canal or 'canal?')


def _rotulo_conv_status(status):
    return {'open': 'aberta', 'pending': 'com o bot'}.get(status, status or '?')


def texto_nota(pedido_code, rotulo_status, detalhe, cid, abertas, estado):
    from app.services.chatbot_vigia import link_chatwoot
    linhas = [f'📦 Pedido {pedido_code} — entrega mudou para: {rotulo_status}'
              + (f' ({detalhe})' if detalhe else '')]
    outras = [c for c in abertas if c != cid]
    if outras:
        partes = []
        for c in outras:
            e = estado.get(c) or {}
            link = link_chatwoot(c)
            partes.append(f'#{c} ({e.get("canal", "canal?")}, '
                          f'{_rotulo_conv_status(e.get("status"))})'
                          + (f' {link}' if link else ''))
        linhas.append('Outras conversas ABERTAS deste pedido: ' + ' · '.join(partes))
        linhas.append('Combine quem avisa o cliente — por UMA conversa só.')
    else:
        linhas.append('Esta é a única conversa aberta deste pedido.')
    linhas.append('(nota automática — o cliente NÃO recebeu mensagem)')
    return '\n'.join(linhas)


def alertar_mudanca_status(pedido_code, status, detalhe=''):
    """Best-effort, em thread. `status` = status do pedido ('a_caminho',
    'entregue') ou rótulo livre ('CORRIDA LALAMOVE CANCELADA'). Devolve
    quantas conversas vinculadas existem (a rede roda depois)."""
    code = _code(pedido_code)
    if not code:
        return {'ok': True, 'conversas': 0}
    try:
        convs = conversas_do_pedido(code)
    except Exception:  # noqa: BLE001
        logger.exception('conversa_pedido: listar conversas do pedido %s falhou', code)
        return {'ok': False, 'conversas': 0}
    if not convs:
        return {'ok': True, 'conversas': 0}
    rotulo = _ROTULO_STATUS.get(status, str(status or '').upper())
    app = current_app._get_current_object()
    _POOL.submit(_executar, app, code, rotulo, (detalhe or '')[:200], convs)
    return {'ok': True, 'conversas': len(convs)}


def _executar(app, code, rotulo, detalhe, convs):
    with app.app_context():
        from app.services import chatwoot
        estado = {}
        for cid in convs:
            try:
                d = chatwoot.consultar_conversa(cid)
            except Exception:  # noqa: BLE001
                logger.exception('conversa_pedido: consultar conv %s falhou', cid)
                d = None
            if d:
                estado[cid] = {'status': d.get('status'), 'canal': _rotulo_canal(d)}
        abertas = [c for c in convs
                   if (estado.get(c) or {}).get('status') in STATUS_ABERTOS]
        avisadas = []
        for cid in abertas:
            texto = texto_nota(code, rotulo, detalhe, cid, abertas, estado)
            try:
                r = chatwoot.enviar_nota_privada(cid, texto)
                if isinstance(r, dict) and r.get('ok'):
                    avisadas.append(cid)
            except Exception:  # noqa: BLE001
                logger.exception('conversa_pedido: nota na conv %s falhou', cid)
        logger.info('conversa_pedido: pedido %s -> %s; abertas=%s avisadas=%s',
                    code, rotulo, abertas, avisadas)
        return {'abertas': abertas, 'avisadas': avisadas}

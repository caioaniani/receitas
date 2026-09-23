"""Vínculo conversa ↔ pedido + aviso à equipe quando o status da entrega
muda (item 8 da spec do dono, caso E3862E49, 22/09/2026).

O pedido E3862E49 gerou três conversas (2429 WhatsApp de saída, 2431
Instagram da compradora, 2432 WhatsApp do marido) e a equipe respondia em
uma sem saber das outras. Agora:

- `vincular(conv_id, pedido_code, origem, autorizada=True)` grava o par
  uma vez, quando o bot identifica o pedido (`consultar_pedido`), quando o
  socorro da falha operacional localiza, quando a equipe clica "Chamar
  cliente" e quando o alerta da Lalamove abre a conversa. Sessão ISOLADA e
  best-effort: roda dentro do turno do bot e de rotas com transação
  própria, e nunca pode derrubá-los. `autorizada=False` = o contato
  perguntou pelo pedido SEM prova de posse (`autorizacao_necessaria`): a
  conversa fica LISTADA pra equipe, mas não recebe a nota com o status.
  Uma autorização posterior na mesma conversa promove o vínculo.
- `alertar_mudanca_status(pedido_code, rotulo, detalhe)` (thread): para
  cada conversa vinculada AUTORIZADA ainda ABERTA (open/pending no
  Chatwoot), nota PRIVADA com o status novo e a lista das outras conversas
  abertas do mesmo pedido (as não autorizadas entram na lista rotuladas).
  NUNCA mensagem ao cliente (contrato: `enviar_nota_privada` é a única
  fala).
"""
import logging
from concurrent.futures import ThreadPoolExecutor

from flask import current_app

logger = logging.getLogger(__name__)

ORIGENS = ('bot', 'socorro', 'chamar', 'lalamove')
STATUS_ABERTOS = ('open', 'pending')
_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='conversa-pedido')
_ROTULO_STATUS = {'a_caminho': 'A CAMINHO', 'entregue': 'ENTREGUE'}
ROTULO_NAO_AUTORIZADA = 'NÃO autorizada — terceiro perguntou por este pedido'


def _conv_ok(conv_id):
    cid = str(conv_id or '').strip()
    return cid if cid.isdigit() and int(cid) > 0 else ''


def _code(pedido_code):
    return str(pedido_code or '').strip().upper()[:40]


def vincular(conv_id, pedido_code, origem, autorizada=True):
    """Grava (conversa, pedido) uma vez. Devolve True se criou agora.
    Par já gravado sem autorização que agora vem autorizado é PROMOVIDO
    (devolve False — não é vínculo novo). Corrida entre dois turnos da
    mesma conversa cai no unique e é tratada como "já existe"."""
    cid, code = _conv_ok(conv_id), _code(pedido_code)
    if not cid or not code:
        return False
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session

    from app.extensions import db
    from app.models import ConversaPedido
    try:
        with Session(db.engine) as s:
            ja = (s.query(ConversaPedido)
                  .filter_by(conv_id=cid, pedido_code=code).first())
            if ja is not None:
                if autorizada and not ja.autorizada:
                    ja.autorizada = True
                    s.commit()
                    logger.info('conversa_pedido: conv %s <-> pedido %s promovido a '
                                'autorizado (%s)', cid, code, origem)
                return False
            s.add(ConversaPedido(conv_id=cid, pedido_code=code,
                                 origem=(origem or '')[:20] or None,
                                 autorizada=bool(autorizada)))
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
                logger.info('conversa_pedido: conv %s <-> pedido %s ja gravado por '
                            'outro turno (corrida)', cid, code)
                return False
        logger.info('conversa_pedido: conv %s <-> pedido %s (%s%s)', cid, code, origem,
                    '' if autorizada else ', NAO autorizada')
        return True
    except Exception:  # noqa: BLE001
        logger.exception('conversa_pedido: vincular conv=%s pedido=%s falhou', cid, code)
        return False


def conversas_do_pedido(pedido_code, detalhado=False):
    """conv_ids vinculados ao pedido, do mais antigo pro mais novo.
    `detalhado=True` devolve dicts {conv_id, autorizada, origem}."""
    from app.models import ConversaPedido
    code = _code(pedido_code)
    if not code:
        return []
    rows = (ConversaPedido.query.filter_by(pedido_code=code)
            .order_by(ConversaPedido.criado_em.asc(), ConversaPedido.id.asc()).all())
    vistos, out = {}, []
    for r in rows:
        if r.conv_id in vistos:
            # Duplicata teorica (unique impede); a autorizacao vale se QUALQUER
            # linha da conversa foi autorizada.
            if r.autorizada:
                vistos[r.conv_id]['autorizada'] = True
            continue
        d = {'conv_id': r.conv_id, 'autorizada': bool(r.autorizada), 'origem': r.origem}
        vistos[r.conv_id] = d
        out.append(d)
    if detalhado:
        return out
    return [d['conv_id'] for d in out]


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
            rotulo = (f'#{c} ({e.get("canal", "canal?")}, '
                      f'{_rotulo_conv_status(e.get("status"))})')
            if not e.get('autorizada', True):
                rotulo += f' — {ROTULO_NAO_AUTORIZADA}'
            partes.append(rotulo + (f' {link}' if link else ''))
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
        convs = conversas_do_pedido(code, detalhado=True)
    except Exception:  # noqa: BLE001
        logger.exception('conversa_pedido: listar conversas do pedido %s falhou', code)
        return {'ok': False, 'conversas': 0}
    if not convs:
        return {'ok': True, 'conversas': 0}
    rotulo = _ROTULO_STATUS.get(status, str(status or '').upper())
    app = current_app._get_current_object()
    _POOL.submit(_executar, app, code, rotulo, (detalhe or '')[:200], convs)
    return {'ok': True, 'conversas': len(convs)}


def _normalizar_convs(convs):
    """Aceita conv_ids soltos (compat) ou os dicts de `conversas_do_pedido`."""
    out = []
    for c in convs or []:
        if isinstance(c, dict):
            cid = _conv_ok(c.get('conv_id'))
            if cid:
                out.append({'conv_id': cid, 'autorizada': bool(c.get('autorizada', True))})
        else:
            cid = _conv_ok(c)
            if cid:
                out.append({'conv_id': cid, 'autorizada': True})
    return out


def _executar(app, code, rotulo, detalhe, convs):
    """Consulta cada conversa vinculada e deixa a nota privada nas ABERTAS e
    AUTORIZADAS. As abertas NÃO autorizadas só entram na lista das outras
    (rotuladas) — nunca recebem o status. Chamado pela thread daqui e, em
    linha, pelo alerta da Lalamove (que já está na thread dele)."""
    with app.app_context():
        from app.services import chatwoot
        itens = _normalizar_convs(convs)
        estado = {}
        for it in itens:
            cid = it['conv_id']
            try:
                d = chatwoot.consultar_conversa(cid)
            except Exception:  # noqa: BLE001
                logger.exception('conversa_pedido: consultar conv %s falhou', cid)
                d = None
            if d:
                estado[cid] = {'status': d.get('status'), 'canal': _rotulo_canal(d),
                               'autorizada': it['autorizada']}
        abertas = [it['conv_id'] for it in itens
                   if (estado.get(it['conv_id']) or {}).get('status') in STATUS_ABERTOS]
        avisadas = []
        for cid in abertas:
            if not estado[cid].get('autorizada', True):
                continue
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

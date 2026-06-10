"""Webhook da Lalamove (status das corridas em tempo real).

URL registrada no portal: https://<host>/lalamove/webhook (Version 3).
Eventos esperados: ORDER_STATUS_CHANGED (data.order.{orderId,status}) e
DRIVER_ASSIGNED (data.driver.{name,phone}).

Autenticidade: o corpo traz `apiKey` — comparamos com a nossa em
`secrets.compare_digest` e exigimos que o `orderId` exista na nossa base
(só atualizamos corridas que NÓS criamos; quem não passa leva 401/200-ignorado).
Os primeiros payloads são logados (truncados) pra validar o formato real
da assinatura HMAC deles e endurecer depois se preciso — decisão explícita,
não silenciosa: o webhook não movimenta dinheiro nem estoque, só espelha
status de exibição.
"""
import logging
import secrets

from flask import jsonify, request

from app.blueprints.lalamove import lalamove_bp
from app.extensions import csrf, db
from app.models import LalamoveEntrega
from app.utils import agora

logger = logging.getLogger(__name__)

csrf.exempt(lalamove_bp)


@lalamove_bp.route('/webhook', methods=['GET', 'POST'])
def webhook():
    # GET = teste de alcance do portal (o "Non-200" some com isso).
    if request.method == 'GET':
        return jsonify(ok=True)

    dados = request.get_json(silent=True) or {}
    logger.info('lalamove webhook: %s', str(dados)[:1000])

    from app.services.lalamove import _cfg
    nossa_key = _cfg('LALAMOVE_API_KEY') or ''
    chave = dados.get('apiKey') or ''
    if not (nossa_key and chave and secrets.compare_digest(chave, nossa_key)):
        logger.warning('lalamove webhook com apiKey invalida — descartado')
        return jsonify(ok=False), 401

    data = dados.get('data') or {}
    ordem = data.get('order') or {}
    order_id = (ordem.get('orderId') or data.get('orderId')
                or dados.get('orderId') or '')
    if not order_id:
        return jsonify(ok=True, ignorado='sem orderId')

    e = LalamoveEntrega.query.filter_by(order_id=str(order_id)).first()
    if not e:
        logger.warning('lalamove webhook de ordem desconhecida: %s', order_id)
        return jsonify(ok=True, ignorado='ordem desconhecida')

    status = (ordem.get('status') or '').upper()
    if status:
        e.status = status
    if ordem.get('shareLink'):
        e.share_link = ordem['shareLink']
    motorista = data.get('driver') or {}
    if motorista.get('name'):
        e.motorista_nome = motorista['name'][:120]
    if motorista.get('phone'):
        e.motorista_telefone = str(motorista['phone'])[:40]
    e.atualizado_em = agora()
    db.session.commit()
    return jsonify(ok=True)

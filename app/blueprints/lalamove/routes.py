"""Webhook da Lalamove (status das corridas em tempo real).

URL registrada no portal: https://<host>/lalamove/webhook (Version 3).
Eventos esperados: ORDER_STATUS_CHANGED (data.order.{orderId,status}) e
DRIVER_ASSIGNED (data.driver.{name,phone}).

Autenticidade: o corpo traz `apiKey` — comparamos com a nossa em
`secrets.compare_digest` e exigimos que o `orderId` exista na nossa base
(só atualizamos corridas que NÓS criamos). Probe de validação do portal
(sem apiKey e sem evento) recebe 200 — não autoriza nada, só diz "estou
vivo"; quem manda apiKey ERRADA leva 401.

Diagnóstico: todo hit é registrado em /tmp (compartilhado entre os workers
do mesmo container) e exposto em /admin/debug-lalamove — assim dá pra saber
se o probe do portal sequer chegou ao servidor.
"""
import json
import logging
import secrets
import time

from flask import jsonify, request

from app.blueprints.lalamove import lalamove_bp
from app.extensions import csrf, db
from app.models import LalamoveEntrega
from app.utils import agora

logger = logging.getLogger(__name__)

csrf.exempt(lalamove_bp)

ARQUIVO_ULTIMO_HIT = '/tmp/lalamove_webhook_ultimo.json'


def _registrar_hit(**info):
    """Melhor esforço: grava o último hit pro debug ler. Nunca quebra o
    webhook por causa disso."""
    try:
        info['quando_epoch'] = time.time()
        info['quando'] = agora().isoformat(sep=' ', timespec='seconds')
        with open(ARQUIVO_ULTIMO_HIT, 'w') as f:
            json.dump(info, f)
    except OSError:  # noqa: BLE001
        logger.warning('nao consegui gravar %s', ARQUIVO_ULTIMO_HIT)


def ultimo_hit():
    """Pro /admin/debug-lalamove: último hit registrado neste container."""
    try:
        with open(ARQUIVO_ULTIMO_HIT) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _salvar_saldo(data, payload_completo):
    """Upsert do saldo (linha única id=1) a partir do evento de carteira.

    Parse liberal: a doc varia entre data.balance.{amount,currency} e campos
    soltos — tenta as chaves conhecidas e guarda o payload cru pra ajustar
    se o formato real for outro (aparece no /admin/debug-lalamove)."""
    from app.models import LalamoveSaldo
    balance = data.get('balance') if isinstance(data.get('balance'), dict) \
        else data
    bruto = (balance.get('amount') or balance.get('totalBalance')
             or balance.get('total') or balance.get('value'))
    valor = None
    try:
        valor = float(str(bruto).replace(',', '.')) if bruto is not None else None
    except ValueError:
        logger.warning('saldo lalamove sem parse: %r', bruto)
    moeda = balance.get('currency') or 'BRL'
    s = db.session.get(LalamoveSaldo, 1) or LalamoveSaldo(id=1)
    if valor is not None:
        s.valor = valor
    s.moeda = moeda
    s.payload_json = json.dumps(payload_completo, ensure_ascii=False)[:4000]
    s.atualizado_em = agora()
    db.session.add(s)
    db.session.commit()
    logger.info('saldo lalamove atualizado: %s %s', moeda, valor)
    # Alerta de saldo baixo (decisao do dono 15/06/2026): se o saldo capturado
    # ficou abaixo do limite (default R$200), dispara WhatsApp pro dono via
    # Z-API. Best-effort — se Z-API estiver fora, o webhook NAO falha (o saldo
    # ja foi persistido). Dedupe anti-spam dentro do servico.
    if valor is not None:
        try:
            from app.services import lalamove_saldo as _saldo_svc
            _saldo_svc.avaliar_e_alertar(valor, moeda)
        except Exception:  # noqa: BLE001
            logger.exception('lalamove_saldo: alerta nao saiu (best-effort)')


@lalamove_bp.route('/webhook', methods=['GET', 'POST', 'HEAD'])
def webhook():
    # GET/HEAD = teste de alcance (portal/navegador).
    if request.method in ('GET', 'HEAD'):
        _registrar_hit(metodo=request.method, tipo='alcance')
        return jsonify(ok=True)

    dados = request.get_json(silent=True) or {}
    chave = dados.get('apiKey') or ''
    event_type = dados.get('eventType') or ''
    data = dados.get('data') or {}
    ordem = data.get('order') or {}
    order_id = (ordem.get('orderId') or data.get('orderId')
                or dados.get('orderId') or '')
    _registrar_hit(metodo='POST', tipo='evento' if order_id else 'ping',
                   tinha_apikey=bool(chave), event_type=event_type)
    logger.info('lalamove webhook: %s', str(dados)[:1000])

    # Probe de validação do portal: sem apiKey e sem evento — responde 200
    # (nao autoriza nada; eventos reais sempre trazem apiKey + orderId).
    if not chave and not order_id:
        return jsonify(ok=True, ping=True)

    from app.services.lalamove import _cfg
    nossa_key = _cfg('LALAMOVE_API_KEY') or ''
    if not (nossa_key and chave and secrets.compare_digest(chave, nossa_key)):
        logger.warning('lalamove webhook com apiKey invalida — descartado')
        return jsonify(ok=False), 401

    # Saldo da carteira: chega a cada debito/recarga. Parse liberal (o
    # formato exato varia na doc) + payload cru guardado pra ajuste fino.
    if event_type == 'WALLET_BALANCE_CHANGED' or (
            'balance' in data and not order_id):
        _salvar_saldo(data, dados)
        return jsonify(ok=True)

    if not order_id:
        return jsonify(ok=True, ignorado='sem orderId')

    e = LalamoveEntrega.query.filter_by(order_id=str(order_id)).first()
    if not e:
        logger.warning('lalamove webhook de ordem desconhecida: %s', order_id)
        return jsonify(ok=True, ignorado='ordem desconhecida')

    # O status do PEDIDO só vem do evento OFICIAL `ORDER_STATUS_CHANGED`.
    # Outros eventos (POP_STATUS_CHANGED = retirada, DRIVER_ASSIGNED, etc.)
    # podem trazer um `status` colateral no payload — se a gente agir nele, o
    # pedido vira "entregue" cedo demais (bug 19/06/2026: marcava entregue na
    # retirada/alocação). Aqui esses eventos só atualizam motorista/link.
    status = ''
    if event_type == 'ORDER_STATUS_CHANGED':
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
    # ENTREGUE = corrida realmente concluída (entregue ao cliente). Só dispara
    # no COMPLETED vindo do ORDER_STATUS_CHANGED — nunca por retirada/alocação.
    if status == 'COMPLETED':
        from app.services.loja_entrega import avancar_status_entrega
        avancar_status_entrega(e.pedido_code, 'entregue')
    return jsonify(ok=True)

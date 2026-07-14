"""Purchase server-side pro GA4 (Measurement Protocol) e Meta (Conversions
API) — 13/07/2026, pedido do dono.

Por quê: o evento `purchase` do navegador só dispara quando o cliente VÊ a
página do pedido como 'pago'. No Pix, muita gente paga no app do banco e
fecha a aba — a venda real acontece (webhook do Pagar.me) mas o funil do
GA4/Meta nunca fica sabendo. Aqui o SERVIDOR reporta a venda na confirmação
do pagamento, à prova de aba fechada.

Dedupe com o evento do navegador (que continua existindo, pois carrega
atribuição de sessão):
- GA4: mesmo `transaction_id` + mesmo `client_id` (capturado do cookie
  `_ga` no checkout → `PedidoOnline.ga_client_id`) — o GA4 deduplica
  purchases idênticos. Sem client_id capturado (cliente recusou cookies →
  o gtag nem carregou no navegador), usamos um id sintético `srv.<id>`;
  nesse caso o evento do navegador nunca disparou, então não há duplicata.
- Meta: `event_id` = código do pedido nos DOIS lados (o fbq do
  pedido_confirmado.html manda o mesmo eventID) — a CAPI deduplica.

Envs (todas opcionais — sem elas o serviço vira no-op logado):
- GA4_ID (já usada pelo gtag) + GA4_API_SECRET (novo: GA4 Admin → Data
  Streams → Measurement Protocol API secrets).
- META_PIXEL_ID (já usada pelo fbq) + META_CAPI_TOKEN (novo: Gerenciador
  de Eventos → pixel → Conversions API → gerar token).
- ANALYTICS_SERVER=0 desliga tudo.

Best-effort ESTRITO: roda em thread própria com sessão isolada; nunca
propaga erro pro fluxo do pagamento (mesmo padrão do frete_sensor).
"""
import hashlib
import logging
import threading

import requests

logger = logging.getLogger(__name__)

_GA4_URL = 'https://www.google-analytics.com/mp/collect'
_META_URL = 'https://graph.facebook.com/v19.0/{pixel_id}/events'
_TIMEOUT = 8


def _ligado(app):
    return (app.config.get('ANALYTICS_SERVER', '1') or '1') != '0'


def ga_client_id_do_cookie(cookie_ga):
    """Extrai o client_id do cookie `_ga` (formato GA1.1.111111.2222222 →
    '111111.2222222'). Devolve None se o cookie não tem a forma esperada."""
    partes = (cookie_ga or '').split('.')
    if len(partes) >= 4 and partes[-2].isdigit() and partes[-1].isdigit():
        return f'{partes[-2]}.{partes[-1]}'
    return None


def _sha256(s):
    return hashlib.sha256(s.encode()).hexdigest()


def _payload_ga4(pedido):
    """Evento purchase do Measurement Protocol — espelha o payload do
    navegador (loja/routes.py::pedido_confirmado) pro dedupe funcionar."""
    client_id = pedido.ga_client_id or f'srv.{pedido.id}'
    return {
        'client_id': client_id,
        'events': [{
            'name': 'purchase',
            'params': {
                'transaction_id': pedido.codigo,
                'value': float(pedido.valor_total or 0),
                'shipping': float(pedido.frete_valor or 0),
                'currency': 'BRL',
                'items': [{
                    'item_id': f'{it.kind}_{it.receita_id or it.produto_id or ""}',
                    'item_name': it.nome,
                    'price': float(it.preco_unitario or 0),
                    'quantity': it.quantidade,
                } for it in pedido.itens],
            },
        }],
    }


def _payload_meta(pedido):
    """Evento Purchase da Conversions API. user_data leva e-mail/telefone
    HASHEADOS (sha256, exigência da Meta — nunca em claro). event_id = código
    do pedido, o MESMO que o fbq do navegador manda (dedupe)."""
    user_data = {}
    email = (pedido.email_cliente or '').strip().lower()
    if email:
        user_data['em'] = [_sha256(email)]
    fone = ''.join(c for c in (pedido.telefone_cliente or '') if c.isdigit())
    if fone:
        if not fone.startswith('55'):
            fone = '55' + fone
        user_data['ph'] = [_sha256(fone)]
    return {
        'data': [{
            'event_name': 'Purchase',
            'event_time': int(pedido.pago_em.timestamp()),
            'event_id': pedido.codigo,
            'action_source': 'website',
            'user_data': user_data,
            'custom_data': {
                'value': float(pedido.valor_total or 0),
                'currency': 'BRL',
            },
        }],
    }


def _enviar_ga4(app, pedido):
    ga4_id = (app.config.get('GA4_ID') or '').strip()
    secret = (app.config.get('GA4_API_SECRET') or '').strip()
    if not ga4_id or not secret:
        logger.info('analytics_server: GA4_ID/GA4_API_SECRET ausentes — '
                    'purchase %s não reportado ao GA4', pedido.codigo)
        return False
    r = requests.post(
        _GA4_URL,
        params={'measurement_id': ga4_id, 'api_secret': secret},
        json=_payload_ga4(pedido), timeout=_TIMEOUT)
    # O MP devolve 2xx mesmo pra payload rejeitado (fire-and-forget da
    # API); status != 2xx aqui é erro de rede/auth de verdade.
    if r.status_code // 100 != 2:
        logger.warning('analytics_server: GA4 respondeu %s pro pedido %s: %s',
                       r.status_code, pedido.codigo, r.text[:200])
        return False
    return True


def _enviar_meta(app, pedido):
    pixel_id = (app.config.get('META_PIXEL_ID') or '').strip()
    token = (app.config.get('META_CAPI_TOKEN') or '').strip()
    if not pixel_id or not token:
        logger.info('analytics_server: META_PIXEL_ID/META_CAPI_TOKEN '
                    'ausentes — purchase %s não reportado à Meta',
                    pedido.codigo)
        return False
    r = requests.post(
        _META_URL.format(pixel_id=pixel_id),
        params={'access_token': token},
        json=_payload_meta(pedido), timeout=_TIMEOUT)
    if r.status_code != 200:
        logger.warning('analytics_server: Meta respondeu %s pro pedido %s: %s',
                       r.status_code, pedido.codigo, r.text[:200])
        return False
    return True


def reportar_purchase(pedido_id):
    """Reporta o purchase de um pedido PAGO ao GA4 e à Meta. Sessão própria
    (nunca toca a transação do chamador); erros só logam."""
    from flask import current_app

    from app.extensions import db
    from app.models import PedidoOnline

    app = current_app._get_current_object()
    if not _ligado(app):
        return {'ga4': False, 'meta': False}
    pedido = db.session.get(PedidoOnline, pedido_id)
    if pedido is None or pedido.pago_em is None:
        return {'ga4': False, 'meta': False}
    out = {'ga4': False, 'meta': False}
    try:
        out['ga4'] = _enviar_ga4(app, pedido)
    except Exception:  # noqa: BLE001 — analytics nunca quebra pagamento
        logger.exception('analytics_server: GA4 falhou (pedido %s)', pedido_id)
    try:
        out['meta'] = _enviar_meta(app, pedido)
    except Exception:  # noqa: BLE001
        logger.exception('analytics_server: Meta falhou (pedido %s)', pedido_id)
    return out


def reportar_purchase_async(pedido_id):
    """Dispara `reportar_purchase` numa thread daemon com app context
    próprio — o webhook do Pagar.me não espera GA/Meta responderem."""
    from flask import current_app
    app = current_app._get_current_object()
    if not _ligado(app):
        return

    def _run():
        with app.app_context():
            try:
                reportar_purchase(pedido_id)
            except Exception:  # noqa: BLE001
                logger.exception('analytics_server: thread falhou (pedido %s)',
                                 pedido_id)

    threading.Thread(target=_run, daemon=True,
                     name=f'analytics-purchase-{pedido_id}').start()

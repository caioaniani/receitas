"""Sincroniza pedidos do site (VNDA) para o cache local `PedidoSite`, usado
pelo card de cliente do CRM (Chatwoot).

Por que cache: a API VNDA filtra pedidos por data de criacao (nao por
telefone) e o telefone so vem apos enriquecer cada pedido (shipping/cliente).
Fazer isso on-demand quando o atendente abre a conversa seria lento e bateria
demais na API (rate limit 429). Entao pre-populamos uma tabela local indexada
por `telefone_chave`, e o card faz lookup instantaneo.

Duas formas de popular (ambas idempotentes por `code`):
- `sincronizar_recentes()` — cron, janela curta (going-forward).
- `backfill(dias)`        — manual (admin), janela longa (historico).

Reusa os helpers de `app.services.vnda` (mesma extracao de telefone/endereco
usada na tela de pedidos), pra nao divergir a logica.
"""
import json
import logging
from datetime import datetime, timedelta

from app.extensions import db
from app.models import PedidoSite
from app.services import vnda
from app.utils import hoje, para_brt, telefone_chave

logger = logging.getLogger(__name__)


def _data_brt(order, *campos):
    """Primeiro campo de data ISO preenchido, convertido pra date em BRT."""
    for c in campos:
        val = order.get(c)
        if not val:
            continue
        try:
            dt = datetime.fromisoformat(str(val).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            continue
        brt = para_brt(dt)
        if brt:
            return brt.date()
    return None


def _contato_pedido(order):
    """(destinatario, telefone) com o MINIMO de chamadas extras à API.

    1) shipping_address embutido na resposta da lista (sem chamada extra)
    2) endpoint dedicado /orders/{code}/shipping_address (cacheado pelo vnda)
    3) recent_address do cliente (cacheado)
    """
    nome, tel, _ = vnda._extrair_endereco(order.get('shipping_address') or {})
    if tel:
        return nome, tel

    code = order.get('code')
    ship = vnda.buscar_shipping_address(code)
    if ship:
        n, t, _ = vnda._extrair_endereco(ship)
        if t:
            return (n or nome), t

    cli = vnda.buscar_cliente(order.get('client_id'))
    if cli:
        n, t, _ = vnda._extrair_endereco(cli.get('recent_address') or {})
        if t:
            return (n or nome), t

    return nome, ''


def _upsert(order):
    """Cria/atualiza um PedidoSite a partir do pedido bruto do VNDA."""
    code = order.get('code')
    if not code:
        return None

    destinatario, tel = _contato_pedido(order)
    itens = [
        {'nome': i.get('product_name') or i.get('name') or '',
         'qtd': i.get('quantity', 1),
         'preco': float(i.get('price') or 0)}
        for i in (order.get('items') or [])
    ]

    reg = PedidoSite.query.get(code) or PedidoSite(code=code)
    reg.telefone = (tel or '')[:50]
    reg.telefone_chave = telefone_chave(tel)
    reg.comprador = (order.get('client_name') or '')[:200]
    reg.destinatario = (destinatario or order.get('client_name') or '')[:200]
    reg.data_pedido = _data_brt(order, 'confirmed_at', 'paid_at', 'created_at')
    reg.data_entrega = vnda._extrair_data_entrega(order)
    reg.total = order.get('total') or 0
    reg.status_vnda = (order.get('status') or '')[:40]
    reg.itens_json = json.dumps(itens, ensure_ascii=False)
    db.session.add(reg)
    return reg


def sincronizar_periodo(start_date, end_date, limite=None):
    """Busca pedidos criados em [start, end] e faz upsert no cache.

    Pula cancelados (nao interessam ao historico do card). Commit em lotes
    de 50 pra nao segurar uma transacao gigante no backfill. Propaga
    `vnda.VndaUnavailableError` se a API cair (caller decide o que fazer).
    """
    raw = vnda._buscar_pedidos_janela(start_date, end_date)
    n = 0
    for order in raw:
        if (order.get('status') or '').lower() in vnda._STATUS_IGNORAR:
            continue
        if _upsert(order) is not None:
            n += 1
            if n % 50 == 0:
                db.session.commit()
        if limite and n >= limite:
            break
    db.session.commit()
    logger.info('vnda_card: %d pedidos sincronizados (%s a %s)', n, start_date, end_date)
    return {'sincronizados': n, 'janela_total': len(raw)}


def sincronizar_recentes(dias=3):
    """Janela curta pro cron going-forward (pedidos novos aparecem no card)."""
    fim = hoje() + timedelta(days=1)
    ini = hoje() - timedelta(days=dias)
    return sincronizar_periodo(ini, fim)


def backfill(dias=365, limite=None):
    """Janela longa, disparada manualmente pelo admin (historico)."""
    fim = hoje() + timedelta(days=3)
    ini = hoje() - timedelta(days=dias)
    return sincronizar_periodo(ini, fim, limite=limite)

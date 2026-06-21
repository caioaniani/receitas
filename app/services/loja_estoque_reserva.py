"""Reserva de estoque pra pedidos da loja online.

Resolve a race condition canonica: 2 clientes comecam checkout do mesmo
produto com 1 unidade em estoque; ambos terminam o checkout; ambos pagam
Pix em 10 minutos; antes da reserva, os 2 webhooks de 'pago' chegavam e o
segundo baixava estoque negativo. Agora a reserva e' tomada NO CHECKOUT
(sob lock pessimista), o catalogo mostra `quantidade - quantidade_reservada`
como disponivel, e o webhook 'pago' apenas CONSOME a reserva.

Modelo:
- `EstoqueLoja.quantidade_reservada` — total reservado AGORA (Integer >= 0).
- `PedidoOnline.reserva_expira_em` — quando a reserva deve cair se o cliente
  abandonou o checkout. Pix expira em 30min; reserva fica 35min (margem
  pro webhook chegar e o cron processar).

Ciclo de vida:
- `reservar(pedido, loja_id)`   -> chamado em `loja_checkout.criar_pedido`
- `consumir(pedido, loja_id)`   -> chamado em `loja_pagamento._marcar_pago`
- `liberar(pedido)`             -> chamado em cancelamento (admin/cliente)
- `liberar_expirados()`         -> chamado em cron 5min (seru_cron)

Cuidados:
- `with_for_update()` em cada linha de EstoqueLoja antes de incrementar
  `quantidade_reservada`. Em Postgres prod, isso serializa as 2 transacoes
  concorrentes. Em SQLite (dev/teste), FOR UPDATE vira no-op silencioso —
  a race ainda existe, mas em dev nao chega a 2 clientes simultaneos.
- NAO commita (deixa pro caller). `criar_pedido` ja envolve tudo numa
  transacao maior; aqui chamamos `flush` so pra forcar o SELECT FOR UPDATE
  pegar a linha real.
- Pedido sem `loja_id` claro (item solto sem FK) NAO reserva — registra
  WARNING igual ao seru_sync e o item passa direto (consumir tambem pula).
"""
import logging
from datetime import timedelta

from app.extensions import db
from app.models import MovEstoqueLoja, PedidoOnline
from app.services import estoque_helpers
from app.utils import agora

logger = logging.getLogger(__name__)

# Margem alem do TTL do Pix (30min) pra o webhook 'pago' chegar e o cron
# liberar quem nunca pagou. 35min = 30min Pix + 5min folga.
TTL_RESERVA_MIN = 35


def _filtro_item(item):
    """Devolve dict pra obter_linha_loja a partir de PedidoOnlineItem.
    None se item solto (sem FK)."""
    if item.receita_id:
        return {'receita_id': item.receita_id}
    if item.produto_id:
        return {'produto_id': item.produto_id}
    return None


def _linha_estoque(loja_id, item, *, lock=True):
    """Pega a linha unica de EstoqueLoja(loja, item), com FOR UPDATE no
    PG. Retorna None se item solto (sem FK)."""
    chave = _filtro_item(item)
    if not chave:
        return None
    el = estoque_helpers.obter_linha_loja(loja_id=loja_id, **chave)
    if lock:
        # Forca o SELECT FOR UPDATE pra serializar com outros checkouts.
        # Em SQLite vira no-op silencioso (nao quebra).
        db.session.refresh(el, with_for_update=True)
    return el


def reservar(pedido, *, loja_id, ttl_min=TTL_RESERVA_MIN):
    """Reserva estoque pra TODOS os itens do pedido. Atomico: ou reserva
    tudo, ou nao reserva nada e devolve a lista de itens sem estoque.

    Retorna dict:
      {'ok': bool, 'sem_estoque': [{'nome', 'pedido', 'disponivel'}], 'reservas': int}

    NAO commita (deixa pro caller dentro da transacao do checkout).
    """
    if not loja_id:
        logger.warning('reservar: sem loja_id (pedido %s)',
                       getattr(pedido, 'codigo', '?'))
        return {'ok': False, 'sem_estoque': [], 'reservas': 0,
                'erro': 'sem_loja'}

    # 1ª passada: trava as linhas e valida — se algum nao bate, abortar
    # ANTES de incrementar. Sem isso, falhar no ultimo item deixaria os
    # primeiros reservados (vazamento de estoque).
    travas = []
    sem_estoque = []
    for it in pedido.itens:
        el = _linha_estoque(loja_id, it, lock=True)
        if el is None:
            # Item solto (sem FK pra receita/produto) — NAO reserva.
            # Igual ao seru_sync: vai logar WARNING no consumir.
            continue
        pedida = int(it.quantidade or 0)
        if pedida <= 0:
            continue
        disp = max(0, (el.quantidade or 0) - (el.quantidade_reservada or 0))
        if disp < pedida:
            sem_estoque.append({
                'nome': it.nome, 'pedido': pedida, 'disponivel': disp,
            })
        else:
            travas.append((el, pedida))

    if sem_estoque:
        # NAO reserva nada — caller mostra erros pro cliente, ele ajusta
        # quantidades e tenta de novo. As linhas que travaram (FOR UPDATE)
        # voltam ao normal no fim da transacao.
        logger.info('reservar: pedido %s sem estoque em %d item(ns)',
                    getattr(pedido, 'codigo', '?'), len(sem_estoque))
        return {'ok': False, 'sem_estoque': sem_estoque, 'reservas': 0}

    # 2ª passada: incrementa quantidade_reservada (as linhas ainda estao
    # com FOR UPDATE).
    for el, pedida in travas:
        el.quantidade_reservada = (el.quantidade_reservada or 0) + pedida

    pedido.reserva_expira_em = agora() + timedelta(minutes=ttl_min)
    db.session.flush()
    logger.info('reservar: pedido %s reservou %d itens (loja %s)',
                getattr(pedido, 'codigo', '?'), len(travas), loja_id)
    return {'ok': True, 'sem_estoque': [], 'reservas': len(travas)}


def consumir(pedido, *, loja_id, usuario_id=None):
    """Chamado no webhook 'pago': baixa estoque DE VERDADE consumindo a
    reserva (decrementa `quantidade` e `quantidade_reservada` juntos).

    Idempotente: se o pedido ja foi consumido (`reserva_expira_em is None`
    e pelo menos um MovEstoqueLoja('venda_site') existe pra ele), no-op.

    Itens sem FK pulam (WARNING). Pode haver shortfall se o cron foi
    burlado e a reserva caiu — registra `venda_site_sem_estoque`,
    igual seru_sync.
    """
    ref = f'Site #{pedido.codigo}'
    # Idempotencia: se ja consumimos, nao fazer de novo (preco da retry
    # do webhook em quase-simultaneo). _marcar_pago em loja_pagamento ja
    # tem FOR UPDATE no pedido, entao chegar aqui 2x deveria ser raro,
    # mas defesa em profundidade nao machuca.
    ja_consumido = (db.session.query(MovEstoqueLoja)
                    .filter(MovEstoqueLoja.tipo == 'venda_site',
                            MovEstoqueLoja.referencia == ref)
                    .first())
    if ja_consumido:
        logger.info('consumir: pedido %s ja consumido (no-op)', pedido.codigo)
        pedido.reserva_expira_em = None
        return {'baixado': 0, 'faltou': 0, 'pulado': 0,
                'ja_consumido': True}

    total = {'baixado': 0, 'faltou': 0, 'pulado': 0}
    for it in pedido.itens:
        el = _linha_estoque(loja_id, it, lock=True)
        if el is None:
            logger.warning('consumir: item solto em pedido %s (%s)',
                           pedido.codigo, it.nome)
            total['pulado'] += 1
            continue
        pedida = int(it.quantidade or 0)
        if pedida <= 0:
            continue
        # Libera a reserva primeiro (mesmo se shortfall — a reserva
        # nao pode segurar mais que existe).
        reservada = (el.quantidade_reservada or 0)
        el.quantidade_reservada = max(0, reservada - pedida)
        # Baixa real.
        atual = el.quantidade or 0
        baixa = min(pedida, atual)
        el.quantidade = atual - baixa
        if baixa > 0:
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=el.id, tipo='venda_site', quantidade=baixa,
                referencia=ref, usuario_id=usuario_id))
            total['baixado'] += baixa
        faltou = pedida - baixa
        if faltou > 0:
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=el.id, tipo='venda_site_sem_estoque',
                quantidade=faltou,
                referencia=f'{ref} — sem estoque suficiente',
                usuario_id=usuario_id))
            total['faltou'] += faltou

    pedido.reserva_expira_em = None
    db.session.flush()
    logger.info('consumir: pedido %s baixou=%d faltou=%d pulado=%d',
                pedido.codigo, total['baixado'], total['faltou'], total['pulado'])
    return total


def liberar(pedido, *, loja_id):
    """Libera a reserva (cancelamento/expiracao). Decrementa
    `quantidade_reservada` por item, devolvendo o saldo virtual.
    Idempotente: pedido sem `reserva_expira_em` = no-op.
    """
    if pedido.reserva_expira_em is None:
        return {'liberadas': 0}

    n = 0
    for it in pedido.itens:
        el = _linha_estoque(loja_id, it, lock=True)
        if el is None:
            continue
        pedida = int(it.quantidade or 0)
        if pedida <= 0:
            continue
        reservada = (el.quantidade_reservada or 0)
        el.quantidade_reservada = max(0, reservada - pedida)
        n += 1
    pedido.reserva_expira_em = None
    db.session.flush()
    logger.info('liberar: pedido %s liberou %d itens', pedido.codigo, n)
    return {'liberadas': n}


def liberar_expirados(*, agora_=None, max_lote=200):
    """Cron 5min: pega pedidos `aguardando_pagamento` com
    `reserva_expira_em < agora` e libera reserva + marca cancelado.

    Idempotente — filtra por status e por reserva_expira_em IS NOT NULL.
    Pega ate `max_lote` por chamada pra nao monopolizar o cron.

    Retorna lista de codigos liberados.
    """
    from app.services.loja_pagamento import _loja_baixa
    base = agora_ or agora()
    q = (PedidoOnline.query
         .filter(PedidoOnline.status == 'aguardando_pagamento',
                 PedidoOnline.reserva_expira_em.isnot(None),
                 PedidoOnline.reserva_expira_em < base)
         .order_by(PedidoOnline.reserva_expira_em)
         .limit(max_lote))
    codigos = []
    for p in q.all():
        loja = _loja_baixa(p)
        if not loja:
            logger.warning('liberar_expirados: pedido %s sem loja origem',
                           p.codigo)
            p.reserva_expira_em = None
            continue
        liberar(p, loja_id=loja.id)
        p.status = 'cancelado'
        p.cancelado_em = base
        codigos.append(p.codigo)
    if codigos:
        db.session.commit()
        logger.info('liberar_expirados: %d pedido(s) cancelado(s): %s',
                    len(codigos), ', '.join(codigos))
    return codigos

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
    """Filtro {col: id} pra obter_linha_loja a partir de um PedidoOnlineItem
    SIMPLES (nao-cesta). None se item solto (sem FK)."""
    if item.receita_id:
        return {'receita_id': item.receita_id}
    if item.produto_id:
        return {'produto_id': item.produto_id}
    return None


def _expandir_estoque(item):
    """Expande um PedidoOnlineItem nas linhas de estoque que ele baixa.

    CESTA (Produto com componentes) -> explode em cada componente
    (`cestas.componentes_de_cesta`): qtd = qtd_comprada x qtd_do_componente,
    com `criar=False` (componente so conta se JA tiver linha de estoque — item
    decorativo/nao-rastreado nao inventa linha nem bloqueia a venda).
    Item simples -> ele mesmo, `criar=True` (comportamento de sempre).
    Item solto (sem FK) ou cesta toda orfa -> [].

    Retorna [(filtro_dict, qtd, nome, criar)]. A MESMA expansao roda em
    reservar/consumir/liberar pra a contabilidade de reserva bater — reserva e
    baixa TEM que mexer exatamente nas mesmas linhas."""
    qtd_compra = int(item.quantidade or 0)
    if qtd_compra <= 0:
        return []
    if item.produto_id:
        from app.models import Produto
        from app.services.cestas import componentes_de_cesta
        cesta = Produto.query.get(item.produto_id)
        if cesta and cesta.itens:                 # e cesta -> explode
            out = []
            for col, cid, nome, qtd_comp in componentes_de_cesta(cesta):
                total = int(round(qtd_compra * float(qtd_comp or 1)))
                if total > 0:
                    out.append(({col: cid}, total, nome, False))
            return out
    chave = _filtro_item(item)
    if not chave:
        return []
    return [(chave, qtd_compra, item.nome, True)]


def _agrega_por_linha(pedido, loja_id, *, lock):
    """Soma a demanda de estoque do pedido POR LINHA de EstoqueLoja (cestas
    explodidas; itens/componentes repetidos somam na mesma linha). Trava as
    linhas (FOR UPDATE) quando `lock`. Componente de cesta sem linha existente
    e ignorado (nao-rastreado). Retorna ([(el, qtd, nome)], n_pulados)."""
    from app.models import EstoqueLoja
    por_linha = {}      # el -> [qtd, nome]
    ordem = []          # ordem estavel de aparicao
    pulados = 0
    for it in pedido.itens:
        expand = _expandir_estoque(it)
        if not expand:
            pulados += 1
            continue
        for filtro, qtd, nome, criar in expand:
            if not criar and not EstoqueLoja.query.filter_by(
                    loja_id=loja_id, **filtro).first():
                continue                          # componente nao rastreado
            el = estoque_helpers.obter_linha_loja(loja_id=loja_id, **filtro)
            if lock:
                # SELECT FOR UPDATE serializa checkouts (no-op em SQLite).
                db.session.refresh(el, with_for_update=True)
            if el not in por_linha:
                por_linha[el] = [0, nome]
                ordem.append(el)
            por_linha[el][0] += qtd
    return [(el, por_linha[el][0], por_linha[el][1]) for el in ordem], pulados


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

    # Agrega a demanda por LINHA (cesta explode nos componentes) e trava
    # (FOR UPDATE). Valida ANTES de incrementar — falhar no fim deixaria
    # linhas ja reservadas (vazamento). Itens repetidos / componentes que
    # caem na mesma linha somam, pra a validacao olhar o total real.
    linhas, _ = _agrega_por_linha(pedido, loja_id, lock=True)
    sem_estoque = []
    for el, qtd, nome, qtd_bloqueia in linhas:
        disp = max(0, (el.quantidade or 0) - (el.quantidade_reservada or 0))
        # So a demanda de item AVULSO (qtd_bloqueia) barra o checkout — e a
        # protecao anti-oversell que justifica a reserva. Componente de cesta
        # (qtd_bloqueia=0) e best-effort: reserva o que der e NAO derruba a
        # venda (a baixa real ja tolera shortfall via venda_site_sem_estoque).
        if qtd_bloqueia > 0 and disp < qtd_bloqueia:
            sem_estoque.append({'nome': nome, 'pedido': qtd_bloqueia,
                                'disponivel': disp})

    if sem_estoque:
        # NAO reserva nada — caller mostra os erros e o cliente ajusta. As
        # linhas travadas voltam ao normal no fim da transacao.
        logger.info('reservar: pedido %s sem estoque em %d linha(s)',
                    getattr(pedido, 'codigo', '?'), len(sem_estoque))
        return {'ok': False, 'sem_estoque': sem_estoque, 'reservas': 0}

    for el, qtd, _nome, _qb in linhas:
        el.quantidade_reservada = (el.quantidade_reservada or 0) + qtd

    pedido.reserva_expira_em = agora() + timedelta(minutes=ttl_min)
    db.session.flush()
    logger.info('reservar: pedido %s reservou %d linha(s) (loja %s)',
                getattr(pedido, 'codigo', '?'), len(linhas), loja_id)
    return {'ok': True, 'sem_estoque': [], 'reservas': len(linhas)}


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
    # mas defesa em profundidade nao machuca. Prefixo: o motor enriquece a
    # referencia (cesta/fracao), entao o mov nem sempre eh exatamente `ref`.
    ja_consumido = (db.session.query(MovEstoqueLoja)
                    .filter(MovEstoqueLoja.tipo == 'venda_site',
                            db.or_(MovEstoqueLoja.referencia == ref,
                                   MovEstoqueLoja.referencia.like(ref + ' %')))
                    .first())
    if ja_consumido:
        logger.info('consumir: pedido %s ja consumido (no-op)', pedido.codigo)
        pedido.reserva_expira_em = None
        return {'baixado': 0, 'faltou': 0, 'pulado': 0,
                'ja_consumido': True}

    # 1. Libera a reserva (mesma agregacao inteira de `reservar`, pra o ledger
    #    de `quantidade_reservada` fechar). Nao depende da baixa real abaixo.
    linhas, pulados = _agrega_por_linha(pedido, loja_id, lock=True)
    for el, qtd, _nome, _qb in linhas:
        el.quantidade_reservada = max(0, (el.quantidade_reservada or 0) - qtd)

    # 2. Baixa real pelo MOTOR UNICO (mesma logica de Seru/lote): explode cesta,
    #    acumula fracao por item, decrementa a linha canonica, gera o movimento.
    #    `pular_sem_linha=True` preserva o comportamento do site de nao baixar
    #    componente decorativo nao-rastreado.
    from app.services.baixa_venda import aplicar_venda
    total = {'baixado': 0, 'faltou': 0, 'pulado': pulados}
    for it in pedido.itens:
        if not (it.receita_id or it.produto_id):
            continue
        res = aplicar_venda(
            loja_id, receita_id=it.receita_id, produto_id=it.produto_id,
            qtd=it.quantidade, canal='site', referencia=ref,
            pedido_ref=f'site:{pedido.codigo}', usuario_id=usuario_id,
            nome_venda=it.nome, pular_sem_linha=True)
        total['baixado'] += res['baixado']
        total['faltou'] += res['faltou']

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

    # Mesma expansao da reserva (cesta -> componentes) — devolve exatamente o
    # que foi reservado, linha a linha.
    linhas, _ = _agrega_por_linha(pedido, loja_id, lock=True)
    for el, qtd, _nome, _qb in linhas:
        el.quantidade_reservada = max(0, (el.quantidade_reservada or 0) - qtd)
    pedido.reserva_expira_em = None
    db.session.flush()
    logger.info('liberar: pedido %s liberou %d linha(s)',
                pedido.codigo, len(linhas))
    return {'liberadas': len(linhas)}


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
        p.motivo_cancelamento = 'pix_expirado'
        p.cancelado_em = base
        codigos.append(p.codigo)
    if codigos:
        db.session.commit()
        logger.info('liberar_expirados: %d pedido(s) cancelado(s): %s',
                    len(codigos), ', '.join(codigos))
    return codigos

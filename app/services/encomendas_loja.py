"""Encomendas do site na TV do padeiro e no pedido da loja (dono, 12/10/2026).

Dono: "Na TV tem que carregar para o padeiro o pedido que vai ser daqui 2
dias, depois o pedido que vai ser para daqui um dia e no dia do pedido tem
que ir para a loja Anesio Pinto Rosa junto com o pedido que a loja faz".
Escolhas dele (AskUserQuestion):

- A TV mostra a encomenda sozinha só de 2 dias antes até a véspera
  (`JANELA_DIAS`); encomenda mais distante fica fora até lá.
- No DIA da entrega ela aparece DENTRO do cartão do pedido da loja de onde o
  site baixa (`loja_pagamento._loja_baixa`: entrega/express = loja de origem
  do site, a Anesio; retirada = a loja que o cliente escolheu). Com mais de
  um pedido da loja no dia, vai no de MENOR id (`pedido_destino`) — o mesmo
  na TV e na saída.
- Quando esse pedido da loja SAI (QR de saída, botão Enviar, copilot), a
  encomenda sai junto: baixa da INDÚSTRIA pelo motor da saída do site
  (`saida_producao_site.registrar`, origem `pedido_loja #<id>`). Não entra no
  estoque da loja — a loja só repassa ao cliente. Voltar o pedido da loja
  de "em transporte" (ou cancelá-lo depois de sair) desfaz essa saída
  (`estornar_saida_junto`).

Fora da regra: pedido de KIT DE CAFÉ (`EntregaKit`, coleta individual na
loja) e cortesia (divulgação baixa na criação) não saem junto. Sem pedido da
loja no dia, o cartão da encomenda continua sozinho na TV. NÃO commita.
"""
import json
import logging
from datetime import timedelta
from decimal import Decimal

from app.extensions import db

logger = logging.getLogger(__name__)

# Quantos dias antes da entrega a encomenda aparece sozinha na TV.
JANELA_DIAS = 2
# Status do pedido do site que ainda depende da produção. A divulgação entra
# (cortesia também é produzida — 23/08/2026).
STATUS_PRODUCAO = ('pago', 'em_preparo', 'a_caminho', 'divulgacao')
# Pedido da loja que ainda vai sair da indústria.
_STATUS_LOJA_ATIVO = ('pendente', 'confirmado', 'separado')
ORIGEM_PEDIDO_LOJA = 'pedido_loja'


def origem_saida(pedido_loja_id):
    return f'{ORIGEM_PEDIDO_LOJA} #{pedido_loja_id}'


def loja_id_da_encomenda(pedido_online, origem_id=None):
    """Loja com cujo pedido a encomenda vai (mesma regra da baixa do site).
    `origem_id` evita reler a configuração a cada encomenda na TV."""
    if pedido_online.modo_entrega == 'retirada' and pedido_online.loja_retirada_id:
        return pedido_online.loja_retirada_id
    if origem_id is not None:
        return origem_id
    from app.services.loja_pagamento import loja_origem_site
    loja = loja_origem_site()
    return loja.id if loja is not None else None


def origem_site_id():
    from app.services.loja_pagamento import loja_origem_site
    loja = loja_origem_site()
    return loja.id if loja is not None else None


def ids_kit_cafe(pedido_ids):
    """Pedidos do site que são entrega de KIT DE CAFÉ (fluxo próprio)."""
    from app.models import EntregaKit
    if not pedido_ids:
        return set()
    return {pid for (pid,) in db.session.query(EntregaKit.pedido_id)
            .filter(EntregaKit.pedido_id.in_(list(pedido_ids))).all()}


def encomendas_ativas(de, ate):
    """PedidoOnline com item sob encomenda e entrega entre `de` e `ate`."""
    from sqlalchemy.orm import selectinload

    from app.models import PedidoOnline, PedidoOnlineItem
    from app.services.loja_estoque_reserva import item_sob_encomenda
    q = (PedidoOnline.query.options(
        selectinload(PedidoOnline.itens).selectinload(PedidoOnlineItem.receita),
        selectinload(PedidoOnline.itens).selectinload(PedidoOnlineItem.produto),
        selectinload(PedidoOnline.itens).selectinload(PedidoOnlineItem.componentes))
        .filter(PedidoOnline.status.in_(STATUS_PRODUCAO),
                PedidoOnline.data_entrega >= de,
                PedidoOnline.data_entrega <= ate)
        .order_by(PedidoOnline.data_entrega, PedidoOnline.id))
    return [p for p in q.all() if any(item_sob_encomenda(it) for it in p.itens)]


def pedido_destino(loja_id, dia):
    """Pedido da loja do dia que leva as encomendas: o de menor id ainda por
    sair. None quando não há."""
    from app.models import PedidoLoja
    return (PedidoLoja.query
            .filter(PedidoLoja.loja_id == loja_id, PedidoLoja.data_entrega == dia,
                    PedidoLoja.status.in_(_STATUS_LOJA_ATIVO))
            .order_by(PedidoLoja.id).first())


def do_pedido_loja(pedido):
    """Encomendas do site que saem junto com este pedido da loja (vazio se
    ele não é o pedido destino do dia)."""
    if pedido.data_entrega is None or pedido.loja_id is None:
        return []
    destino = pedido_destino(pedido.loja_id, pedido.data_entrega)
    if destino is None or destino.id != pedido.id:
        return []
    origem_id = origem_site_id()
    encomendas = [p for p in encomendas_ativas(pedido.data_entrega, pedido.data_entrega)
                  if not p.divulgacao
                  and loja_id_da_encomenda(p, origem_id) == pedido.loja_id]
    kits = ids_kit_cafe([p.id for p in encomendas])
    return [p for p in encomendas if p.id not in kits]


def registrar_saida_junto(pedido, usuario_id):
    """Registra a saída da indústria das encomendas que vão com o pedido da
    loja. Chamar ANTES de travar o pedido da loja (ordem da saída do site:
    trava global → pedido do site → estoque; a loja vem depois).

    Devolve {'enviadas': [codigos], 'problemas': [(codigo, motivo)]}. Uma
    encomenda com problema (ex.: composição sem vínculo) não impede a saída
    do pedido da loja — continua na TV e o motivo volta para a mensagem."""
    from app.services.saida_producao_site import itens_baixados, registrar
    enviadas, problemas = [], []
    for p in do_pedido_loja(pedido):
        antes = itens_baixados(p.id)
        try:
            with db.session.begin_nested():
                registrar(p, origem_saida(pedido.id), usuario_id)
        except ValueError as exc:
            logger.warning('Encomenda %s não saiu com o pedido #%s: %s',
                           p.codigo, pedido.id, exc)
            problemas.append((p.codigo, str(exc)))
            continue
        if itens_baixados(p.id) - antes:
            enviadas.append(p.codigo)
    return {'enviadas': enviadas, 'problemas': problemas}


def estornar_saida_junto(pedido, usuario_id, motivo='voltar status'):
    """Desfaz a saída das encomendas que foram com este pedido da loja (ele
    voltou de "em transporte" ou foi cancelado depois de sair): devolve à
    indústria o que foi baixado e apaga o registro de saída, para a encomenda
    voltar a aparecer na TV e sair de novo no próximo envio. Devolve os
    códigos das encomendas."""
    from app.models import (
        MateriaPrima,
        MovEstoqueProducao,
        MovimentacaoEstoque,
        PedidoOnline,
        SaidaProducaoSite,
    )
    from app.services.estoque_congelados import obter_linha_producao
    saidas = (SaidaProducaoSite.query
              .filter_by(origem=origem_saida(pedido.id))
              .order_by(SaidaProducaoSite.pedido_item_id)
              .with_for_update().all())
    codigos = []
    for s in saidas:
        po = db.session.get(PedidoOnline, s.pedido_id)
        codigo = po.codigo if po else str(s.pedido_id)
        ref = f'Estorno encomenda site {codigo} (pedido #{pedido.id}: {motivo})'
        try:
            componentes = json.loads(s.componentes_json or '[]')
        except ValueError:
            componentes = []
        for c in componentes:
            qtd = Decimal(str(c.get('baixado') or '0'))
            if qtd <= 0:
                continue
            col, cid = c.get('coluna'), c.get('id')
            if col == 'materia_prima_id':
                mp = (MateriaPrima.query.filter_by(id=cid)
                      .with_for_update().populate_existing().first())
                if mp is None:
                    continue
                mp.estoque_atual = float(Decimal(str(mp.estoque_atual or 0)) + qtd)
                db.session.add(MovimentacaoEstoque(
                    materia_prima_id=cid, tipo='entrada', quantidade=float(qtd),
                    referencia=ref, usuario_id=usuario_id))
            elif col in ('receita_id', 'produto_id'):
                ep = obter_linha_producao(**{col: cid}, usuario_id=usuario_id)
                ep.quantidade = int(ep.quantidade or 0) + int(qtd)
                db.session.add(MovEstoqueProducao(
                    estoque_producao_id=ep.id, tipo='estorno_saida_pedido',
                    quantidade=int(qtd), referencia=ref, usuario_id=usuario_id))
        db.session.delete(s)
        if codigo not in codigos:
            codigos.append(codigo)
    db.session.flush()
    return codigos


def texto_saida_junto(res):
    """Frase para a mensagem da saída do pedido da loja ('' se nada)."""
    if not res:
        return ''
    partes = []
    if res.get('enviadas'):
        partes.append('Encomendas do site que foram junto: '
                      + ', '.join(res['enviadas']) + '.')
    for codigo, motivo in res.get('problemas') or []:
        partes.append(f'A encomenda {codigo} NÃO saiu com o pedido: {motivo}')
    return ' '.join(partes)


def janela_tv(base):
    return base, base + timedelta(days=JANELA_DIAS)

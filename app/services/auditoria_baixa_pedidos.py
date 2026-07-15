"""Auditoria: a saída do pedido loja→indústria baixou os congelados?

Pergunta do dono (14/07/2026, "está dando diferença no estoque"): para cada
PedidoLoja que JÁ SAIU da indústria (em_transporte/entregue/recebido), compara
o que o pedido pedia com o que os movimentos `MovEstoqueProducao` registram
de verdade (mesma família de referência do motor único de baixa —
`pedido_estoque._ref_base`: 'Pedido #<id> → <loja>').

Read-only estrito. Classificação por pedido:
- ok              baixado líquido + falta registrada == esperado
- com_falta       idem, mas parte saiu como `saida_pedido_sem_estoque`
                  (caminhão saiu sem saldo — diferença LEGÍTIMA e visível)
- sem_movimento   pedido saiu e NENHUM movimento existe (escapou da baixa —
                  pedido antigo pré-motor-único ou bug)
- divergente      movimentos existem mas não fecham com o pedido

Itens sem FK (só nome, legado) não têm baixa possível — contados à parte.
"""
from datetime import timedelta

from sqlalchemy import func

from app.extensions import db
from app.models import (
    EstoqueProducao,
    MovEstoqueProducao,
    MovimentacaoEstoque,
    PedidoLoja,
)
from app.utils import hoje

_STATUS_SAIU = ('em_transporte', 'entregue', 'recebido')


def _movs_do_pedido(pedido_id):
    """Somas por tipo dos movimentos de produção do pedido (pela referência
    do motor único). O ' →' logo após o número impede colisão #1 vs #10."""
    ref_like = f'Pedido #{pedido_id} →%'
    rows = (db.session.query(MovEstoqueProducao.tipo,
                             func.sum(MovEstoqueProducao.quantidade))
            .filter(MovEstoqueProducao.referencia.like(ref_like))
            .group_by(MovEstoqueProducao.tipo).all())
    por_tipo = {t: int(s or 0) for t, s in rows}
    estornos = (db.session.query(func.sum(MovEstoqueProducao.quantidade))
                .filter(MovEstoqueProducao.tipo.in_(
                    ('estorno_saida_pedido', 'ajuste')),
                    MovEstoqueProducao.referencia.like(
                        f'Estorno pedido #{pedido_id} %'))
                .scalar())
    mp_baixada = (db.session.query(func.sum(MovimentacaoEstoque.quantidade))
                  .filter(MovimentacaoEstoque.tipo == 'saida',
                          MovimentacaoEstoque.referencia.like(ref_like))
                  .scalar())
    return {
        'baixado': por_tipo.get('saida_pedido', 0),
        'falta_registrada': por_tipo.get('saida_pedido_sem_estoque', 0),
        'estornado': int(estornos or 0),
        'mp_baixada': float(mp_baixada or 0),
    }


def auditar(dias=14, max_detalhe=50):
    corte = hoje() - timedelta(days=max(1, dias) - 1)
    pedidos = (PedidoLoja.query
               .filter(PedidoLoja.status.in_(_STATUS_SAIU),
                       db.or_(PedidoLoja.data_entrega >= corte,
                              PedidoLoja.data_pedido >= corte))
               .order_by(PedidoLoja.id.desc()).all())

    resumo = {'pedidos_analisados': len(pedidos), 'ok': 0, 'com_falta': 0,
              'sem_movimento': 0, 'divergente': 0, 'so_mp': 0,
              'itens_sem_fk': 0}
    problemas = []
    for p in pedidos:
        esperado = 0
        esperado_mp = 0.0
        sem_fk = []
        for it in p.itens:
            qtd = it.quantidade or 0
            if qtd <= 0:
                continue
            if it.materia_prima_id:
                esperado_mp += float(qtd)
            elif it.receita_id or it.produto_id:
                esperado += int(qtd)
            else:
                sem_fk.append(it.item_nome or f'item #{it.id}')
        resumo['itens_sem_fk'] += len(sem_fk)

        m = _movs_do_pedido(p.id)
        liquido = m['baixado'] - m['estornado']
        contado = liquido + m['falta_registrada']

        if esperado == 0 and esperado_mp > 0:
            status = 'so_mp'          # pedido só de MP — baixa é na MP
        elif contado == esperado and m['falta_registrada'] == 0:
            status = 'ok'
        elif contado == esperado:
            status = 'com_falta'
        elif m['baixado'] == 0 and m['falta_registrada'] == 0:
            status = 'sem_movimento'
        else:
            status = 'divergente'
        resumo[status] += 1

        if status in ('sem_movimento', 'divergente', 'com_falta') or sem_fk:
            if len(problemas) < max_detalhe:
                problemas.append({
                    'pedido_id': p.id,
                    'loja': p.loja.nome if p.loja else None,
                    'status_pedido': p.status,
                    'data_pedido': p.data_pedido.isoformat()
                    if p.data_pedido else None,
                    'data_entrega': p.data_entrega.isoformat()
                    if p.data_entrega else None,
                    'classificacao': status,
                    'esperado_unidades': esperado,
                    'baixado': m['baixado'],
                    'estornado': m['estornado'],
                    'falta_registrada': m['falta_registrada'],
                    'esperado_mp': esperado_mp,
                    'mp_baixada': m['mp_baixada'],
                    'itens_sem_fk': sem_fk,
                })

    # Faltas agregadas por ITEM no período — a fonte mais comum da
    # "diferença": o caminhão saiu sem saldo e o sistema registrou a falta.
    corte_dt = corte
    faltas_rows = (db.session.query(EstoqueProducao,
                                    func.sum(MovEstoqueProducao.quantidade))
                   .join(MovEstoqueProducao,
                         MovEstoqueProducao.estoque_producao_id
                         == EstoqueProducao.id)
                   .filter(MovEstoqueProducao.tipo
                           == 'saida_pedido_sem_estoque',
                           func.date(MovEstoqueProducao.data) >= corte_dt)
                   .group_by(EstoqueProducao.id).all())
    faltas_por_item = sorted(
        ({'item': ep.nome_item, 'faltou': int(s or 0)}
         for ep, s in faltas_rows if s),
        key=lambda x: -x['faltou'])

    return {'dias': dias, 'inicio': corte.isoformat(),
            'resumo': resumo, 'pedidos_problema': problemas,
            'faltas_por_item': faltas_por_item}

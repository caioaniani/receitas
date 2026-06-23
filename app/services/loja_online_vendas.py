"""Vendas da loja propria (PedidoOnline) pros relatorios/faturamento.

Substitui o VNDA como fonte das vendas do site a partir do cutover
(22/06/2026): o VNDA foi desligado e a loja nativa (`PedidoOnline`) passou
a ser a origem das vendas online. Espelha a semantica do `vnda_sync` pra
encaixar nas agregacoes existentes sem quebrar a comparacao com o Seru:

- **Venda** = pedido PAGO e NAO cancelado (`pago_em` preenchido + status !=
  'cancelado'). Pedido em 'aguardando_pagamento' nao conta; 'cancelado'
  (estornado) sai.
- **Faturamento** = `subtotal` (soma dos itens, EXCLUI frete) — mesma base
  do VNDA/Seru, que nao tem frete, pra os numeros baterem.
- **Data de venda** = `pago_em` (em BRT, ja gravado por `agora()`).

Dinheiro vem em `Numeric/Decimal` do modelo; aqui devolve `float`
arredondado no MESMO formato que `vnda_sync` entrega pros callers.
"""
from datetime import datetime, time, timedelta

from app.extensions import db
from app.models import PedidoOnline, PedidoOnlineItem


def _intervalo_pago(data_inicial, data_final):
    """Filtro SQLAlchemy: pedidos pagos (por `pago_em` BRT) no intervalo
    [data_inicial, data_final] (inclusivo), nao cancelados.

    Usa limites de datetime (>= inicio 00:00, < fim+1 00:00) em vez de
    `func.date()` — dialect-safe (SQLite e Postgres) e a comparacao com
    NULL ja exclui pedidos nao pagos."""
    ini_dt = datetime.combine(data_inicial, time.min)
    fim_dt = datetime.combine(data_final + timedelta(days=1), time.min)
    return (PedidoOnline.pago_em >= ini_dt,
            PedidoOnline.pago_em < fim_dt,
            PedidoOnline.status != 'cancelado')


def faturamento_por_dia(data_inicial, data_final):
    """Faturamento da loja propria por DATA DE VENDA (`pago_em`).

    Espelha `vnda_sync.faturamento_por_dia`. Faturamento = `subtotal`
    (sem frete). Retorna {'total': float, 'n_pedidos': int,
    'por_dia': {date: float}}.
    """
    pedidos = (PedidoOnline.query
               .filter(*_intervalo_pago(data_inicial, data_final))
               .all())
    total = 0.0
    por_dia = {}
    n = 0
    for p in pedidos:
        val = float(p.subtotal or 0)
        if val <= 0:
            continue
        dv = p.pago_em.date()
        total += val
        por_dia[dv] = por_dia.get(dv, 0.0) + val
        n += 1
    return {
        'total': round(total, 2),
        'n_pedidos': n,
        'por_dia': {d: round(v, 2) for d, v in por_dia.items()},
    }


def vendas_por_produto(data_inicial, data_final):
    """{(tipo, id): qtd} dos itens vendidos (pedidos pagos no intervalo).

    Espelha `vendas_manuais._agregar_vendas_vnda_api` pro consolidado.
    `tipo` = 'receita'|'produto'. Item sem FK (catalogo solto) eh pulado.
    NAO desempacota cestas — conta o produto como foi comprado (uma cesta =
    uma cesta); o cliente comprou aquilo, nao os componentes.
    """
    rows = (db.session.query(PedidoOnlineItem)
            .join(PedidoOnline, PedidoOnlineItem.pedido_id == PedidoOnline.id)
            .filter(*_intervalo_pago(data_inicial, data_final))
            .all())
    agg = {}
    for it in rows:
        if it.receita_id:
            chave = ('receita', it.receita_id)
        elif it.produto_id:
            chave = ('produto', it.produto_id)
        else:
            continue
        agg[chave] = agg.get(chave, 0) + int(it.quantidade or 0)
    return agg


def contagem_para_dia(dia):
    """Pra cada item da producao, quanto temos que entregar no dia X — somando
    pedidos do site (PedidoOnline pago/em_preparo/a_caminho/entregue) com
    data_entrega=X. CESTAS sao DESEMPACOTADAS: 5 Family Box × 1 sourdough
    cada = +5 sourdoughs na contagem. Cancelados e aguardando_pagamento
    ficam de fora — so o que vai mesmo virar producao.

    Retorna [{'tipo': 'receita'|'produto'|'mp', 'id': int, 'nome': str,
              'qtd': int, 'detalhes': [...]}] ordenado por qtd desc.
    """
    from collections import defaultdict

    from app.models import Produto
    from app.services.vnda_sync import _componentes_de_cesta

    pedidos = (PedidoOnline.query
               .filter(PedidoOnline.data_entrega == dia,
                       PedidoOnline.status.in_(
                           ('pago', 'em_preparo', 'a_caminho', 'entregue')))
               .all())

    # (tipo, id) -> {'nome': str, 'qtd': int, 'detalhes': [(origem, qtd)]}
    # `detalhes` ajuda a auditar "de onde veio a qtd": ex `[('Family Box', 5),
    # ('venda direta', 2)]` mostra que 5 sairam de cestas e 2 da venda solta.
    agg = defaultdict(lambda: {'nome': '', 'qtd': 0, 'detalhes': []})

    for p in pedidos:
        for it in p.itens:
            qtd_compra = int(round(float(it.quantidade or 0)))
            if qtd_compra <= 0:
                continue
            # Se for cesta (Produto com itens), explode.
            cesta = None
            if it.produto_id:
                cesta = Produto.query.get(it.produto_id)
                if cesta and not cesta.itens:
                    cesta = None  # produto simples (sem composicao)
            if cesta:
                origem = cesta.nome
                for tipo_c, id_c, nome_c, qtd_no_item in _componentes_de_cesta(cesta):
                    if not id_c:
                        continue
                    total = int(round(qtd_compra * float(qtd_no_item or 1)))
                    if total <= 0:
                        continue
                    e = agg[(tipo_c, id_c)]
                    e['nome'] = nome_c
                    e['qtd'] += total
                    e['detalhes'].append((origem, total))
            else:
                # Item direto (receita ou produto simples)
                if it.receita_id:
                    chave = ('receita', it.receita_id)
                elif it.produto_id:
                    chave = ('produto', it.produto_id)
                else:
                    continue
                e = agg[chave]
                e['nome'] = it.nome
                e['qtd'] += qtd_compra
                e['detalhes'].append(('venda direta', qtd_compra))

    out = []
    for (tipo, id_), v in agg.items():
        out.append({
            'tipo': tipo, 'id': id_,
            'nome': v['nome'], 'qtd': v['qtd'],
            'detalhes': v['detalhes'],
        })
    out.sort(key=lambda x: (-x['qtd'], x['nome']))
    return out


def produtos_vendidos(data_inicial, data_final):
    """Lista por produto pra a visao da loja do site (espelha o formato de
    `vnda_sync.agregar_vendas`): [{nome, sku, qtd, tipo, id}] + total_pedidos.

    Usa a FK do item (receita_id/produto_id) — sem fuzzy match, ja que a
    loja propria grava o vinculo direto. Retorna
    {'produtos': [...], 'total_pedidos': int}.
    """
    pedidos = (PedidoOnline.query
               .filter(*_intervalo_pago(data_inicial, data_final))
               .all())
    agg = {}  # chave -> {nome, tipo, id, qtd}
    for p in pedidos:
        for it in p.itens:
            if it.receita_id:
                chave = ('receita', it.receita_id)
            elif it.produto_id:
                chave = ('produto', it.produto_id)
            else:
                continue
            e = agg.setdefault(chave, {
                'nome': it.nome, 'tipo': chave[0], 'id': chave[1], 'qtd': 0})
            e['qtd'] += int(it.quantidade or 0)
    produtos = sorted(agg.values(), key=lambda x: -x['qtd'])
    return {'produtos': produtos, 'total_pedidos': len(pedidos)}

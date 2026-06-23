"""Previsao de demanda diaria por item × loja.

Versao MVP: media das ultimas 4 ocorrencias do mesmo dia-da-semana,
baseado em MovEstoqueLoja com tipos de venda de loja (Seru + VNDA).
Sem ARIMA/Prophet — pra 30 SKUs, media por dow ja captura sazonalidade
semanal e e auditavel (admin entende como o numero saiu).

Roadmap se precisar evoluir:
- Suavizacao exponencial pra dar mais peso aos ultimos
- Detecao de outlier (feriado, evento)
- Confidence interval
"""
from collections import defaultdict
from datetime import date, timedelta

from app.constants import VENDA_TIPOS_LOJA


def prever_demanda(loja_id, data_alvo, semanas_lookback=8):
    """Retorna lista [{tipo_item, item_id, nome, previsao, observacoes_n,
    historico}] pra todos os itens com venda nos ultimos `semanas_lookback`
    semanas naquela loja.

    Previsao = media de quantas unidades sairam do item, nos ultimos
    N domingos / segundas / etc (dependendo do dow do data_alvo).
    """
    from app.extensions import db
    from app.models import EstoqueLoja, MovEstoqueLoja

    if not loja_id:
        return []
    if isinstance(data_alvo, str):
        data_alvo = date.fromisoformat(data_alvo)
    dow_alvo = data_alvo.weekday()  # 0=seg, 6=dom
    desde = data_alvo - timedelta(days=semanas_lookback * 7)

    # Pra cada (estoque_loja_id, dia_da_semana), acumula qtd vendida por dia.
    # vendas_por_item_dia[(eloja_id, data)] = qtd
    vendas_por_dia = defaultdict(int)
    movs = (db.session.query(MovEstoqueLoja)
            .join(EstoqueLoja, MovEstoqueLoja.estoque_loja_id == EstoqueLoja.id)
            .filter(EstoqueLoja.loja_id == loja_id,
                    MovEstoqueLoja.tipo.in_(VENDA_TIPOS_LOJA),
                    MovEstoqueLoja.data >= desde,
                    MovEstoqueLoja.data < data_alvo)
            .all())
    for m in movs:
        d = m.data.date() if hasattr(m.data, 'date') else m.data
        vendas_por_dia[(m.estoque_loja_id, d)] += int(m.quantidade or 0)

    # Agora separa por (eloja_id, dow) e mantem so o dow_alvo.
    por_item_dow = defaultdict(list)
    for (eloja_id, d), qtd in vendas_por_dia.items():
        if d.weekday() == dow_alvo:
            por_item_dow[eloja_id].append((d, qtd))

    # Carrega nome de cada EstoqueLoja num so query.
    elojas_ids = list(por_item_dow.keys())
    if not elojas_ids:
        return []
    elojas = {el.id: el for el in EstoqueLoja.query.filter(
        EstoqueLoja.id.in_(elojas_ids)
    ).all()}

    out = []
    for eloja_id, pontos in por_item_dow.items():
        el = elojas.get(eloja_id)
        if not el:
            continue
        nome = el.nome_item if hasattr(el, 'nome_item') else '?'
        # tipo (receita / produto / mp / pendente)
        if el.receita_id:
            tipo, item_id = 'receita', el.receita_id
        elif el.produto_id:
            tipo, item_id = 'produto', el.produto_id
        elif el.materia_prima_id:
            tipo, item_id = 'mp', el.materia_prima_id
        else:
            tipo, item_id = 'pendente', el.id

        qtds = [q for _, q in pontos]
        media = sum(qtds) / len(qtds) if qtds else 0
        out.append({
            'tipo_item': tipo,
            'item_id': item_id,
            'nome': nome,
            'previsao': round(media, 1),
            'observacoes_n': len(qtds),
            'historico': sorted(pontos, reverse=True),  # ultimas no topo
            'estoque_atual': el.quantidade or 0,
        })
    # Ordena por previsao decrescente (item mais vendido em primeiro)
    out.sort(key=lambda x: -x['previsao'])
    return out


def prever_semana(loja_id, data_inicio=None):
    """Retorna {data: [itens previstos]} pros proximos 7 dias."""
    if data_inicio is None:
        from app.utils import hoje as _hoje_brt
        data_inicio = _hoje_brt() + timedelta(days=1)  # amanha
    elif isinstance(data_inicio, str):
        data_inicio = date.fromisoformat(data_inicio)
    out = {}
    for i in range(7):
        d = data_inicio + timedelta(days=i)
        out[d.isoformat()] = prever_demanda(loja_id, d)
    return out


def prever_pedido_por_loja(semanas=3, data_ref=None, loja_id=None):
    """Previsao do PEDIDO DE REPOSICAO por loja, baseada no historico de
    PedidoLoja das ultimas `semanas` semanas (media semanal por item).

    DIFERENTE de prever_demanda (que usa venda POS / MovEstoqueLoja): aqui a
    base e o que cada loja PEDIU (PedidoLoja). Esse e o processo real de
    reposicao do dono e existe pra TODA loja, inclusive as que nao tem PDV
    (caso 23/06/2026: dono pediu "previsao de pedidos das 3 lojas
    considerando as ultimas 3 semanas" e o bot despejou o historico cru em
    vez de calcular). Conta determinista, server-side.

    Janela: pedidos com `data_entrega` em [data_ref - semanas*7, data_ref).
    Exclui cancelados. Retorna dict:
      {loja_id: {'loja_nome', 'pedidos_considerados', 'semanas', 'desde',
                 'ate', 'itens': [{'nome', 'total', 'media_semanal',
                 'sugerido'}]}}
    `media_semanal` = total / semanas; `sugerido` = ceil(media_semanal)
    (o que pedir pra proxima semana). Itens ordenados por media desc.
    """
    import math
    from collections import defaultdict

    from app.models import PedidoLoja
    from app.utils import hoje as _hoje_brt

    semanas = max(1, int(semanas or 1))
    if data_ref is None:
        data_ref = _hoje_brt()
    elif isinstance(data_ref, str):
        data_ref = date.fromisoformat(data_ref)
    desde = data_ref - timedelta(days=semanas * 7)

    q = (PedidoLoja.query
         .filter(PedidoLoja.data_entrega >= desde,
                 PedidoLoja.data_entrega < data_ref,
                 PedidoLoja.status != 'cancelado'))
    if loja_id:
        q = q.filter(PedidoLoja.loja_id == loja_id)
    pedidos = q.all()

    por_loja_item = defaultdict(lambda: defaultdict(int))  # lid -> nome -> qtd
    por_loja_nome = {}
    por_loja_npedidos = defaultdict(int)
    for p in pedidos:
        lid = p.loja_id
        por_loja_nome[lid] = p.loja.nome if p.loja else '?'
        por_loja_npedidos[lid] += 1
        for it in p.itens:
            por_loja_item[lid][it.nome_item] += int(it.quantidade or 0)

    out = {}
    for lid, itens in por_loja_item.items():
        lista = []
        for nome, total in itens.items():
            media = total / semanas
            lista.append({
                'nome': nome,
                'total': total,
                'media_semanal': round(media, 1),
                'sugerido': int(math.ceil(media)),
            })
        lista.sort(key=lambda x: -x['media_semanal'])
        out[lid] = {
            'loja_nome': por_loja_nome.get(lid, '?'),
            'pedidos_considerados': por_loja_npedidos[lid],
            'semanas': semanas,
            'desde': desde.isoformat(),
            'ate': data_ref.isoformat(),
            'itens': lista,
        }
    return out

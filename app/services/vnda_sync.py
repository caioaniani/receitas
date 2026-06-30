"""Sincronizacao VNDA → estoque (Loja Anesio Pinto Rosa, fixa).

Diferente do Seru: baixa quando o pedido tem expected_delivery_date = hoje,
independente do status (pago/preparado/entregue). Cancelados depois geram
estorno automatico.

Idempotencia: VndaPedidoProcessado por `code` (codigo do pedido VNDA).
"""
import logging
from datetime import timedelta

from app.models import (
    AppConfig,
    Loja,
    VndaProdutoMap,
)
from app.services import vnda

logger = logging.getLogger(__name__)

# Default usado se admin nao configurou ainda em /pdv/vnda/mapeamentos.
LOJA_VNDA_NOME_DEFAULT = 'Loja Anesio Pinto Rosa'
STATUS_CANCELADO = {'canceled', 'cancelled'}


def loja_vnda():
    """Retorna a Loja configurada pra receber as baixas VNDA.
    Le de AppConfig.vnda_loja_id; se nao houver, faz fallback pelo nome
    default ('Loja Anesio Pinto Rosa')."""
    loja_id = AppConfig.get_int('vnda_loja_id')
    if loja_id:
        l = Loja.query.get(loja_id)
        if l:
            return l
    return Loja.query.filter_by(nome=LOJA_VNDA_NOME_DEFAULT).first()


def faturamento_por_dia(data_inicial, data_final):
    """Faturamento VNDA (site) por DATA DE VENDA, espelhando a semantica do
    Seru (que conta pela data do `createdAt`/venda).

    Diferente de `agregar_vendas` (que agrupa por data de ENTREGA, pra estoque):
    aqui a chave eh a data de venda = `confirmed_at`/`paid_at` convertido pra
    BRT via `seru.data_local` (reuso — evita o bug do `_parse_iso_date`, que
    devolve a data UTC). Faturamento = soma de `item['subtotal']` (receita de
    produto; EXCLUI frete e descontos de pedido), pra comparar com o PDV/Seru
    que nao tem frete. Trocar por `order['total']` se um dia quiser com frete.

    Janela da API folgada (+/- 2 dias) porque o filtro start/finish da VNDA eh
    por data de criacao do pedido e a borda UTC/BRT pode jogar a venda pro dia
    vizinho; o filtro fino fica no Python (por data de venda BRT).

    Retorna {'total': float, 'n_pedidos': int, 'por_dia': {date: float}}.
    Levanta `vnda.VndaUnavailableError` se a API falhar (o caller decide se
    trata como best-effort).
    """
    from app.services import seru

    todos = vnda._buscar_pedidos_janela(
        data_inicial - timedelta(days=2), data_final + timedelta(days=2))

    total = 0.0
    por_dia = {}
    n_pedidos = 0
    for order in todos:
        if not isinstance(order, dict):
            continue
        if (order.get('status') or '').lower() in STATUS_CANCELADO:
            continue
        dv = seru.data_local(order.get('confirmed_at') or order.get('paid_at'))
        if not dv or not (data_inicial <= dv <= data_final):
            continue
        val_pedido = 0.0
        for item in order.get('items') or []:
            if not isinstance(item, dict):
                continue
            try:
                sub = float(item.get('subtotal') or 0)
            except (TypeError, ValueError):
                sub = 0.0
            if sub <= 0:  # fallback: preco unitario x quantidade
                try:
                    sub = float(item.get('price') or 0) * float(item.get('quantity') or 0)
                except (TypeError, ValueError):
                    sub = 0.0
            if sub > 0:
                val_pedido += sub
        if val_pedido <= 0:
            continue
        total += val_pedido
        por_dia[dv] = por_dia.get(dv, 0.0) + val_pedido
        n_pedidos += 1
    return {
        'total': round(total, 2),
        'n_pedidos': n_pedidos,
        'por_dia': {d: round(v, 2) for d, v in por_dia.items()},
    }


def agregar_vendas(data_inicial, data_final):
    """Agrega vendas VNDA por produto no periodo (por data de ENTREGA).

    Espelha `vendas_itens.agregar_itens` (que eh do Seru) pra alimentar a
    reconciliacao. Retorna {produtos: [...], total_pedidos, total_itens,
    loja} ou {erro: ...} se a API VNDA falhar.
    """
    loja = loja_vnda()
    start = data_inicial - timedelta(days=60)
    end = data_final + timedelta(days=3)
    try:
        todos = vnda._buscar_pedidos_janela(start, end)
    except vnda.VndaUnavailableError as e:
        return {'erro': f'vnda_indisponivel: {e}'}
    except Exception as e:  # noqa: BLE001
        logger.exception('agregar_vendas vnda falhou')
        return {'erro': f'{type(e).__name__}: {str(e)[:200]}'}

    agg = {}
    total_pedidos = 0
    for order in todos:
        if not isinstance(order, dict):
            continue
        de = vnda._extrair_data_entrega(order)
        if not de or not (data_inicial <= de <= data_final):
            continue
        if (order.get('status') or '').lower() in STATUS_CANCELADO:
            continue
        code = (order.get('code') or '').strip()
        contou = False
        for item in order.get('items') or []:
            if not isinstance(item, dict):
                continue
            nome = (item.get('product_name') or item.get('name') or '').strip()
            try:
                qtd = float(item.get('quantity', 0) or 0)
            except (TypeError, ValueError):
                qtd = 0.0
            if not nome or qtd <= 0:
                continue
            sku = (item.get('sku') or item.get('product_sku') or '').strip() or None
            e = agg.setdefault(nome, {'qtd': 0.0, 'sku': sku, 'pedidos': set()})
            e['qtd'] += qtd
            if sku and not e['sku']:
                e['sku'] = sku
            e['pedidos'].add(code)
            contou = True
        if contou:
            total_pedidos += 1

    maps = {}
    if agg:
        maps = {m.vnda_nome: m for m in VndaProdutoMap.query.filter(
            VndaProdutoMap.vnda_nome.in_(list(agg.keys()))).all()}

    produtos = []
    for nome, v in agg.items():
        m = maps.get(nome)
        if m:
            estado = m.estado
            mapeado_para = {
                'tipo': 'receita' if m.receita_id else ('produto' if m.produto_id else None),
                'id': m.receita_id or m.produto_id,
                'nome': m.alvo_nome,
            } if estado == 'mapeado' else None
            fator = float(m.fator_quantidade or 1.0)
        else:
            estado = 'sem_map'
            mapeado_para = None
            fator = 1.0
        produtos.append({
            'nome': nome, 'sku': v['sku'], 'qtd': v['qtd'],
            'n_pedidos': len(v['pedidos']), 'estado_map': estado,
            'mapeado_para': mapeado_para, 'fator': fator,
        })
    produtos.sort(key=lambda x: x['qtd'], reverse=True)
    return {
        'produtos': produtos,
        'total_pedidos': total_pedidos,
        'total_itens': sum(p['qtd'] for p in produtos),
        'loja': loja.nome if loja else None,
    }


_COL_PRA_TIPO = {
    'receita_id': 'receita',
    'produto_id': 'produto',
    'materia_prima_id': 'mp',
}


def _componentes_de_cesta(produto):
    """Wrapper sobre `cestas.componentes_de_cesta` adaptando o tipo curto.

    Retorna [(tipo_curto, id, item_nome, quantidade_no_item)] com
    tipo_curto = 'receita' | 'produto' | 'mp'. A funcao canonica retorna
    o nome da coluna; convertemos pra evitar quebrar `_baixar_item` que
    ja consome o tipo curto.
    """
    from app.services.cestas import componentes_de_cesta
    out = []
    for col, item_id, nome, qtd in componentes_de_cesta(produto):
        tipo_curto = _COL_PRA_TIPO.get(col, 'mp')
        out.append((tipo_curto, item_id, nome, qtd))
    return out


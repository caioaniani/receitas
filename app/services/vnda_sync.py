"""Sincronizacao VNDA → estoque (Loja Anesio Pinto Rosa, fixa).

Diferente do Seru: baixa quando o pedido tem expected_delivery_date = hoje,
independente do status (pago/preparado/entregue). Cancelados depois geram
estorno automatico.

Idempotencia: VndaPedidoProcessado por `code` (codigo do pedido VNDA).
"""
import logging
from datetime import timedelta

from app.extensions import db
from app.models import (
    AppConfig,
    EstoqueLoja,
    Loja,
    MateriaPrima,
    MovEstoqueLoja,
    Receita,
    VndaDebito,
    VndaPedidoProcessado,
    VndaProdutoMap,
)
from app.services import vnda
from app.utils import agora

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


def _resolver_produto(vnda_nome, vnda_sku):
    """Pega/cria VndaProdutoMap. Pendente na primeira aparicao."""
    if not vnda_nome:
        return None
    mp = VndaProdutoMap.query.filter_by(vnda_nome=vnda_nome).first()
    if mp:
        if vnda_sku and not mp.vnda_sku:
            mp.vnda_sku = vnda_sku
        return mp
    mp = VndaProdutoMap(vnda_nome=vnda_nome, vnda_sku=vnda_sku or None)
    db.session.add(mp)
    db.session.flush()
    return mp


def _componentes_de_cesta(produto):
    """Wrapper sobre `cestas.componentes_de_cesta` adaptando o tipo curto.

    Retorna [(tipo_curto, id, item_nome, quantidade_no_item)] com
    tipo_curto = 'receita' | 'mp'. A funcao canonica retorna o nome da
    coluna ('receita_id' | 'materia_prima_id'); convertemos pra evitar
    quebrar o caller `_baixar_item` que ja consome o tipo curto.
    """
    from app.services.cestas import componentes_de_cesta
    out = []
    for col, item_id, nome, qtd in componentes_de_cesta(produto):
        tipo_curto = 'receita' if col == 'receita_id' else 'mp'
        out.append((tipo_curto, item_id, nome, qtd))
    return out


def _baixar_componente(loja_id, mp, componente_key, tipo, item_id,
                       a_baixar_float, vnda_code, user_id, label=''):
    """Baixa UM componente com seu proprio acumulador fracionario."""
    if not item_id:
        return {'baixado': False, 'faltou': a_baixar_float}

    filtro = {'loja_id': loja_id}
    if tipo == 'receita':
        filtro['receita_id'] = item_id
    elif tipo == 'produto':
        filtro['produto_id'] = item_id
    elif tipo == 'mp':
        filtro['materia_prima_id'] = item_id
    else:
        return {'baixado': False, 'faltou': 0}

    debito = VndaDebito.query.filter_by(
        vnda_produto_map_id=mp.id, componente_key=componente_key).first()
    if not debito:
        debito = VndaDebito(vnda_produto_map_id=mp.id,
                            componente_key=componente_key, fracao_pendente=0.0)
        db.session.add(debito)
        db.session.flush()
    debito_total = (debito.fracao_pendente or 0.0) + a_baixar_float
    inteiros = int(debito_total + 1e-9)
    debito.fracao_pendente = max(0.0, round(debito_total - inteiros, 6))

    if inteiros <= 0:
        return {'baixado': False, 'faltou': 0}

    el = EstoqueLoja.query.filter_by(**filtro).first()
    if not el:
        el = EstoqueLoja(**filtro, quantidade=0)
        db.session.add(el)
        db.session.flush()

    atual = el.quantidade or 0
    real = min(inteiros, atual)
    falta = inteiros - real

    if real > 0:
        el.quantidade = atual - real
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo='venda_vnda', quantidade=real,
            referencia=f'VNDA #{vnda_code}{label}', usuario_id=user_id,
        ))
    if falta > 0:
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id, tipo='venda_vnda_sem_estoque', quantidade=falta,
            referencia=f'VNDA #{vnda_code}{label} — sem estoque suficiente',
            usuario_id=user_id,
        ))
    return {'baixado': real > 0, 'faltou': falta}


def _baixar_item(loja_id, mp, qtd, vnda_code, user_id):
    """Aplica baixa considerando fator e CESTAS.

    Se mp aponta pra Produto que e cesta (tem ProdutoItens), explode em
    multiplas baixas — cada componente baixa proporcionalmente. Cada
    componente tem seu proprio acumulador via `componente_key`.
    """
    fator = float(mp.fator_quantidade or 1.0)
    qtd_efetiva = float(qtd) * fator
    ref_extra = '' if fator == 1.0 else f' (fator {fator})'

    componentes = _componentes_de_cesta(mp.produto) if mp.produto_id else []

    if componentes:
        # Cesta: baixa cada componente
        algum_baixou = False
        falta_total = 0
        for tipo, item_id, item_nome, qtd_no_item in componentes:
            a_baixar = qtd_efetiva * qtd_no_item
            ckey = f'{tipo[0]}:{item_id}'
            label = f'{ref_extra} [{item_nome}]'
            res = _baixar_componente(loja_id, mp, ckey, tipo, item_id,
                                      a_baixar, vnda_code, user_id, label=label)
            if res['baixado']:
                algum_baixou = True
            falta_total += res.get('faltou', 0)
        return {'baixado': algum_baixou, 'faltou': falta_total}

    # Produto simples: 1 baixa direta
    if mp.receita_id:
        tipo, item_id = 'receita', mp.receita_id
    elif mp.produto_id:
        tipo, item_id = 'produto', mp.produto_id
    else:
        return {'baixado': False, 'faltou': qtd}
    return _baixar_componente(loja_id, mp, 'self', tipo, item_id,
                               qtd_efetiva, vnda_code, user_id, label=ref_extra)


def _estornar_pedido(reg, user_id):
    """Reverte baixas de um pedido VNDA cancelado."""
    movs = MovEstoqueLoja.query.filter(
        MovEstoqueLoja.tipo == 'venda_vnda',
        MovEstoqueLoja.referencia.like(f'VNDA #{reg.vnda_pedido_code}%'),
    ).all()
    for m in movs:
        el = EstoqueLoja.query.get(m.estoque_loja_id)
        if el:
            el.quantidade = (el.quantidade or 0) + m.quantidade
            db.session.add(MovEstoqueLoja(
                estoque_loja_id=el.id,
                tipo='venda_vnda_estorno',
                quantidade=m.quantidade,
                referencia=f'Estorno VNDA #{reg.vnda_pedido_code} (cancelada)',
                usuario_id=user_id,
            ))
    reg.estornado_em = agora()


def processar_pedidos(data_entrega, user=None):
    """Sincroniza pedidos VNDA com data de entrega = `data_entrega` (BRT).

    Janela de busca: 60 dias pra tras + 3 a frente (pedidos VNDA podem ser
    agendados com muita antecedencia). Filtra localmente pelo expected
    delivery date e processa so os do dia alvo.
    """
    user_id = getattr(user, 'id', None) if user else None

    loja = loja_vnda()
    if not loja:
        return {'erro': 'Loja VNDA nao configurada. Configure em /pdv/vnda/mapeamentos.',
                'erros': []}

    start = data_entrega - timedelta(days=60)
    end = data_entrega + timedelta(days=3)

    try:
        todos = vnda._buscar_pedidos_janela(start, end)
    except vnda.VndaUnavailableError as e:
        return {'erro': f'vnda_indisponivel: {e}', 'erros': []}
    except Exception as e:
        logger.exception('vnda_sync: erro ao buscar pedidos')
        return {'erro': f'{type(e).__name__}: {str(e)[:300]}', 'erros': []}

    stats = {
        'pedidos_novos': 0,
        'pedidos_ja_processados': 0,
        'pedidos_cancelados_estornados': 0,
        'itens_baixados': 0,
        'itens_ignorados': 0,
        'itens_pendentes_novos': 0,
        'itens_sem_estoque': 0,
        'erros': [],
    }

    for order in todos:
        if not isinstance(order, dict):
            continue
        de = vnda._extrair_data_entrega(order)
        if de != data_entrega:
            continue
        code = (order.get('code') or '').strip()
        if not code:
            continue

        is_canceled = (order.get('status') or '').lower() in STATUS_CANCELADO
        reg = VndaPedidoProcessado.query.get(code)

        if reg:
            stats['pedidos_ja_processados'] += 1
            if is_canceled and not reg.estornado_em:
                _estornar_pedido(reg, user_id)
                reg.cancelado_em = agora()
                stats['pedidos_cancelados_estornados'] += 1
            continue

        # Pedido novo
        items = order.get('items') or []
        if is_canceled:
            db.session.add(VndaPedidoProcessado(
                vnda_pedido_code=code,
                data_entrega=de,
                cancelado_em=agora(),
                n_itens_total=len(items),
                n_itens_baixados=0,
            ))
            continue

        n_total = len(items)
        n_baixados = 0

        for item in items:
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
            mp = _resolver_produto(nome, sku)
            if not mp:
                continue
            if mp.ignorar:
                stats['itens_ignorados'] += 1
                continue
            if mp.estado == 'pendente':
                stats['itens_pendentes_novos'] += 1
                continue
            res = _baixar_item(loja.id, mp, qtd, code, user_id)
            if res['baixado']:
                stats['itens_baixados'] += 1
                n_baixados += 1
            if res['faltou']:
                stats['itens_sem_estoque'] += 1

        db.session.add(VndaPedidoProcessado(
            vnda_pedido_code=code,
            data_entrega=de,
            n_itens_total=n_total,
            n_itens_baixados=n_baixados,
        ))
        stats['pedidos_novos'] += 1

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.exception('vnda_sync commit falhou')
        stats['erros'].append(f'commit: {type(e).__name__}: {str(e)[:200]}')

    return stats

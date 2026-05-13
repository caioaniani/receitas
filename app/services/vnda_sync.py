"""Sincronizacao VNDA → estoque (Loja Anesio Pinto Rosa, fixa).

Diferente do Seru: baixa quando o pedido tem expected_delivery_date = hoje,
independente do status (pago/preparado/entregue). Cancelados depois geram
estorno automatico.

Idempotencia: VndaPedidoProcessado por `code` (codigo do pedido VNDA).
"""
import logging
from datetime import date, datetime, timedelta

from app.extensions import db
from app.models import (Loja, EstoqueLoja, MovEstoqueLoja,
                        VndaProdutoMap, VndaPedidoProcessado, VndaDebito)
from app.services import vnda

logger = logging.getLogger(__name__)

LOJA_VNDA_NOME = 'Loja Anesio Pinto Rosa'
STATUS_CANCELADO = {'canceled', 'cancelled'}


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


def _baixar_item(loja_id, mp, qtd, vnda_code, user_id):
    """Aplica baixa em EstoqueLoja(loja Anesio) considerando fator_quantidade
    com acumulador fracionario (mesma logica do Seru)."""
    filtro = {'loja_id': loja_id}
    if mp.receita_id:
        filtro['receita_id'] = mp.receita_id
    elif mp.produto_id:
        filtro['produto_id'] = mp.produto_id
    else:
        return {'baixado': False, 'faltou': qtd}

    fator = float(mp.fator_quantidade or 1.0)
    a_baixar_float = float(qtd) * fator

    # Acumulador: soma a fracao pendente, separa inteiros pra baixar agora
    debito = VndaDebito.query.filter_by(vnda_produto_map_id=mp.id).first()
    if not debito:
        debito = VndaDebito(vnda_produto_map_id=mp.id, fracao_pendente=0.0)
        db.session.add(debito)
        db.session.flush()
    debito_total = (debito.fracao_pendente or 0.0) + a_baixar_float
    inteiros = int(debito_total + 1e-9)
    debito.fracao_pendente = max(0.0, round(debito_total - inteiros, 6))

    if inteiros <= 0:
        return {'baixado': False, 'faltou': 0, 'acumulado': debito.fracao_pendente}

    el = EstoqueLoja.query.filter_by(**filtro).first()
    if not el:
        el = EstoqueLoja(**filtro, quantidade=0)
        db.session.add(el)
        db.session.flush()

    atual = el.quantidade or 0
    real = min(inteiros, atual)
    falta = inteiros - real
    ref_extra = '' if fator == 1.0 else f' (fator {fator})'

    if real > 0:
        el.quantidade = atual - real
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id,
            tipo='venda_vnda',
            quantidade=real,
            referencia=f'VNDA #{vnda_code}{ref_extra}',
            usuario_id=user_id,
        ))
    if falta > 0:
        db.session.add(MovEstoqueLoja(
            estoque_loja_id=el.id,
            tipo='venda_vnda_sem_estoque',
            quantidade=falta,
            referencia=f'VNDA #{vnda_code}{ref_extra} — sem estoque suficiente',
            usuario_id=user_id,
        ))
    return {'baixado': real > 0, 'faltou': falta, 'acumulado': debito.fracao_pendente}


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
    reg.estornado_em = datetime.utcnow()


def processar_pedidos(data_entrega, user=None):
    """Sincroniza pedidos VNDA com data de entrega = `data_entrega` (BRT).

    Janela de busca: 60 dias pra tras + 3 a frente (pedidos VNDA podem ser
    agendados com muita antecedencia). Filtra localmente pelo expected
    delivery date e processa so os do dia alvo.
    """
    user_id = getattr(user, 'id', None) if user else None

    loja = Loja.query.filter_by(nome=LOJA_VNDA_NOME).first()
    if not loja:
        return {'erro': f'Loja "{LOJA_VNDA_NOME}" nao encontrada', 'erros': []}

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
                reg.cancelado_em = datetime.utcnow()
                stats['pedidos_cancelados_estornados'] += 1
            continue

        # Pedido novo
        items = order.get('items') or []
        if is_canceled:
            db.session.add(VndaPedidoProcessado(
                vnda_pedido_code=code,
                data_entrega=de,
                cancelado_em=datetime.utcnow(),
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

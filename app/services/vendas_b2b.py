"""Vendas B2B da industria.

Encapsula a logica de:
- Criar venda (cabecalho + itens + parcelas) com baixa de EstoqueProducao
- Cancelar venda (estorna estoque + marca parcelas canceladas)
- Receber pagamento (atualiza parcela, calcula saldo)

Estoque sai do EstoqueProducao (industria/freezer). Quando falta saldo,
registra MovEstoqueProducao tipo='venda_b2b_sem_estoque' (igual logica
das vendas Seru) — sai mesmo assim e fica como auditoria.
"""
from datetime import datetime, timedelta
from decimal import Decimal

from app.extensions import db
from app.models import (VendaB2B, VendaB2BItem, VendaB2BParcela,
                        EstoqueProducao, MovEstoqueProducao,
                        ClienteB2B, Receita, Produto)
from app.utils import agora, hoje


def _get_or_create_estoque(receita_id=None, produto_id=None):
    """Acha (ou cria zerado) a linha de EstoqueProducao do item."""
    filtro = {
        'receita_id': receita_id,
        'produto_id': produto_id,
    }
    ep = EstoqueProducao.query.filter_by(**filtro).first()
    if not ep:
        ep = EstoqueProducao(**filtro, quantidade=0)
        db.session.add(ep)
        db.session.flush()
    return ep


def criar_venda(*, cliente_id=None, cliente_nome=None, data_venda=None,
                itens, parcelas=None, observacao=None, nf_numero=None,
                user=None):
    """Cria venda B2B + itens + parcelas + baixa estoque.

    itens: lista de {tipo: 'receita'|'produto', id, quantidade,
                     preco_unitario, desconto_percentual}
    parcelas: lista de {vencimento (date), valor, forma_pagamento (str)}
              ou None pra criar 1 parcela unica ao total

    Returns: VendaB2B persistida.
    """
    if not itens:
        raise ValueError('venda sem itens')
    if not cliente_id and not (cliente_nome or '').strip():
        raise ValueError('cliente obrigatorio (cadastrado ou avulso)')

    venda = VendaB2B(
        data_venda=data_venda or hoje(),
        cliente_id=cliente_id,
        cliente_nome=(cliente_nome or '').strip() or None,
        observacao=(observacao or '').strip() or None,
        nf_numero=(nf_numero or '').strip() or None,
        criado_por_id=getattr(user, 'id', None),
        valor_total=0,
    )
    db.session.add(venda)
    db.session.flush()

    total = 0.0
    for it in itens:
        tipo = it.get('tipo')
        item_id = it.get('id')
        try:
            qtd = int(it.get('quantidade') or 0)
        except (TypeError, ValueError):
            qtd = 0
        if qtd <= 0:
            continue
        if tipo not in ('receita', 'produto') or not item_id:
            continue
        try:
            preco = float(it.get('preco_unitario') or 0)
        except (TypeError, ValueError):
            preco = 0
        try:
            desc = float(it.get('desconto_percentual') or 0)
        except (TypeError, ValueError):
            desc = 0

        vi = VendaB2BItem(
            venda_id=venda.id,
            receita_id=item_id if tipo == 'receita' else None,
            produto_id=item_id if tipo == 'produto' else None,
            quantidade=qtd,
            preco_unitario=preco,
            desconto_percentual=desc,
        )
        db.session.add(vi)
        total += vi.valor_total

        # Baixa do EstoqueProducao
        ep = _get_or_create_estoque(
            receita_id=item_id if tipo == 'receita' else None,
            produto_id=item_id if tipo == 'produto' else None,
        )
        saldo = ep.quantidade or 0
        baixa = min(qtd, saldo)
        ep.quantidade = saldo - baixa

        if baixa > 0:
            db.session.add(MovEstoqueProducao(
                estoque_producao_id=ep.id,
                tipo='venda_b2b',
                quantidade=baixa,
                referencia=f'Venda B2B #{venda.id} ({venda.cliente_display})',
                usuario_id=getattr(user, 'id', None),
            ))
        if qtd > baixa:
            falta = qtd - baixa
            db.session.add(MovEstoqueProducao(
                estoque_producao_id=ep.id,
                tipo='venda_b2b_sem_estoque',
                quantidade=falta,
                referencia=f'Venda B2B #{venda.id} sem saldo (faltou {falta})',
                usuario_id=getattr(user, 'id', None),
            ))

    venda.valor_total = round(total, 2)

    # Parcelas
    if not parcelas:
        # 1 parcela unica com vencimento = hoje
        db.session.add(VendaB2BParcela(
            venda_id=venda.id, numero=1,
            vencimento=venda.data_venda,
            valor=venda.valor_total,
        ))
    else:
        for n, p in enumerate(parcelas, start=1):
            venc = p.get('vencimento')
            if isinstance(venc, str):
                from datetime import date
                venc = date.fromisoformat(venc)
            db.session.add(VendaB2BParcela(
                venda_id=venda.id, numero=n,
                vencimento=venc,
                valor=float(p.get('valor') or 0),
                forma_pagamento=(p.get('forma_pagamento') or '').strip() or None,
            ))

    db.session.commit()
    return venda


def cancelar_venda(venda, user=None):
    """Estorna estoque e marca venda como cancelada. Idempotente."""
    if venda.status == 'cancelada':
        return venda
    for vi in venda.itens:
        receita_id = vi.receita_id
        produto_id = vi.produto_id
        if not (receita_id or produto_id):
            continue
        ep = EstoqueProducao.query.filter_by(
            receita_id=receita_id, produto_id=produto_id,
        ).first()
        if not ep:
            continue
        ep.quantidade = (ep.quantidade or 0) + vi.quantidade
        db.session.add(MovEstoqueProducao(
            estoque_producao_id=ep.id,
            tipo='venda_b2b_estorno',
            quantidade=vi.quantidade,
            referencia=f'Estorno venda B2B #{venda.id}',
            usuario_id=getattr(user, 'id', None),
        ))
    venda.status = 'cancelada'
    venda.cancelado_em = agora()
    venda.cancelado_por_id = getattr(user, 'id', None)
    db.session.commit()
    return venda


def receber_pagamento(parcela, valor, forma_pagamento=None, observacao=None):
    """Soma valor ao valor_pago da parcela. Marca pago_em se quitar."""
    try:
        v = float(valor)
    except (TypeError, ValueError):
        raise ValueError('valor invalido')
    if v <= 0:
        raise ValueError('valor deve ser > 0')
    parcela.valor_pago = (parcela.valor_pago or 0) + v
    if forma_pagamento:
        parcela.forma_pagamento = forma_pagamento
    if observacao:
        parcela.observacao = observacao
    if parcela.valor_pago >= parcela.valor:
        parcela.pago_em = agora()
    db.session.commit()
    return parcela


def preco_sugerido(receita_id=None, produto_id=None, cliente=None):
    """Retorna preco atacado + desconto do cliente aplicado.

    Retorna float ou None se nao houver preco cadastrado.
    """
    if not receita_id and not produto_id:
        return None
    pa = PrecoAtacado.query.filter_by(
        receita_id=receita_id, produto_id=produto_id,
    ).first()
    if not pa:
        return None
    preco = pa.preco_unitario
    if cliente and cliente.desconto_percentual:
        preco = preco * (1 - cliente.desconto_percentual / 100.0)
    return round(preco, 2)

"""Vendas B2B da industria.

Encapsula a logica de:
- Criar venda (cabecalho + itens + parcelas) com baixa de EstoqueProducao
- Cancelar venda (estorna estoque + marca parcelas canceladas)
- Receber pagamento (atualiza parcela, calcula saldo)

Estoque sai do EstoqueProducao (industria/freezer). Quando falta saldo,
registra MovEstoqueProducao tipo='venda_b2b_sem_estoque' (igual logica
das vendas Seru) — sai mesmo assim e fica como auditoria.
"""

from app.extensions import db
from app.models import (
    EstoqueProducao,
    MovEstoqueProducao,
    Produto,
    Receita,
    VendaB2B,
    VendaB2BItem,
    VendaB2BParcela,
)
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
                data_entrega=None, itens, parcelas=None, observacao=None,
                nf_numero=None, user=None):
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

    from decimal import Decimal
    total = Decimal('0')
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
            preco = Decimal(str(it.get('preco_unitario') or 0))
        except (TypeError, ValueError):
            preco = Decimal('0')
        try:
            desc = float(it.get('desconto_percentual') or 0)
        except (TypeError, ValueError):
            desc = 0.0

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

        # CESTA: se Produto tem componentes, baixa cada um do EstoqueProducao
        # em vez do produto inteiro (industria so estoca componentes).
        componentes_cesta = []
        if tipo == 'produto':
            from app.services.cestas import componentes_de_cesta
            produto_obj = Produto.query.get(item_id)
            componentes_cesta = componentes_de_cesta(produto_obj)

        if componentes_cesta:
            for col, comp_id, nome_comp, qtd_por_cesta in componentes_cesta:
                qtd_baixar = int(round(qtd * qtd_por_cesta))
                if qtd_baixar <= 0:
                    continue
                ep_c = _get_or_create_estoque(
                    receita_id=comp_id if col == 'receita_id' else None,
                    produto_id=None,  # so receita/mp na producao
                )
                saldo_c = ep_c.quantidade or 0
                baixa_c = min(qtd_baixar, saldo_c)
                ep_c.quantidade = saldo_c - baixa_c
                if baixa_c > 0:
                    db.session.add(MovEstoqueProducao(
                        estoque_producao_id=ep_c.id,
                        tipo='venda_b2b',
                        quantidade=baixa_c,
                        referencia=(f'Venda B2B #{venda.id} '
                                    f'[{produto_obj.nome} → cesta] {nome_comp}'),
                        usuario_id=getattr(user, 'id', None),
                    ))
                if qtd_baixar > baixa_c:
                    falta_c = qtd_baixar - baixa_c
                    db.session.add(MovEstoqueProducao(
                        estoque_producao_id=ep_c.id,
                        tipo='venda_b2b_sem_estoque',
                        quantidade=falta_c,
                        referencia=(f'Venda B2B #{venda.id} '
                                    f'[{produto_obj.nome} → cesta] {nome_comp} — faltou {falta_c}'),
                        usuario_id=getattr(user, 'id', None),
                    ))
            continue  # ja registrou; pula a baixa normal

        # Baixa do EstoqueProducao (produto/receita normal)
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

    from decimal import ROUND_HALF_UP
    venda.valor_total = total.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

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
                valor=Decimal(str(p.get('valor') or 0)),
                forma_pagamento=(p.get('forma_pagamento') or '').strip() or None,
            ))

    db.session.commit()
    return venda


def cancelar_venda(venda, user=None):
    """Estorna estoque e marca venda como cancelada. Idempotente.

    Le as `MovEstoqueProducao` reais geradas em `criar_venda` (filtra por
    `referencia LIKE 'Venda B2B #{id}%'` e `tipo=='venda_b2b'`) e restaura
    EXATAMENTE o que foi baixado. Cobre 3 cenarios que a versao anterior
    quebrava:
      1) Cesta — baixa foi nos componentes; antes buscava `produto_id=cesta`
         e nao achava, deixando estoque baixado para sempre.
      2) Falta de estoque parcial — antes somava `vi.quantidade` (total),
         gerando estoque do nada quando havia `venda_b2b_sem_estoque`.
      3) `venda_b2b_sem_estoque` puro — nao toca, nao deve criar nada.
    """
    if venda.status == 'cancelada':
        return venda

    movs = MovEstoqueProducao.query.filter(
        MovEstoqueProducao.tipo == 'venda_b2b',
        MovEstoqueProducao.referencia.like(f'Venda B2B #{venda.id}%'),
    ).all()
    for m in movs:
        ep = EstoqueProducao.query.get(m.estoque_producao_id)
        if not ep:
            continue
        ep.quantidade = (ep.quantidade or 0) + m.quantidade
        db.session.add(MovEstoqueProducao(
            estoque_producao_id=ep.id,
            tipo='venda_b2b_estorno',
            quantidade=m.quantidade,
            referencia=f'Estorno venda B2B #{venda.id} (cancelada)',
            usuario_id=getattr(user, 'id', None),
        ))
    venda.status = 'cancelada'
    venda.cancelado_em = agora()
    venda.cancelado_por_id = getattr(user, 'id', None)
    db.session.commit()
    return venda


def receber_pagamento(parcela, valor, forma_pagamento=None, observacao=None):
    """Soma valor ao valor_pago da parcela. Marca pago_em se quitar.

    Numeric(10, 2) no banco garante precisao exata em centavos —
    sem necessidade de tolerancia de arredondamento.
    """
    from decimal import Decimal, InvalidOperation
    try:
        v = Decimal(str(valor))
    except (TypeError, ValueError, InvalidOperation):
        raise ValueError('valor invalido')
    if v <= 0:
        raise ValueError('valor deve ser > 0')

    pago_atual = Decimal(parcela.valor_pago or 0)
    parcela.valor_pago = pago_atual + v

    if forma_pagamento:
        parcela.forma_pagamento = forma_pagamento
    if observacao:
        parcela.observacao = observacao

    if parcela.valor_pago >= Decimal(parcela.valor or 0):
        parcela.pago_em = agora()
    db.session.commit()
    return parcela


def preco_sugerido(receita_id=None, produto_id=None, cliente=None):
    """Retorna preco atacado do cadastro + desconto do cliente aplicado.

    Receita usa `preco_venda`, Produto usa `preco_atacado` (mesma logica
    de /cardapio?tipo=atacado). Retorna float ou None se nao houver preco.
    """
    preco = None
    if receita_id:
        r = Receita.query.get(receita_id)
        preco = r.preco_venda if r else None
    elif produto_id:
        p = Produto.query.get(produto_id)
        preco = p.preco_atacado if p else None
    if not preco:
        return None
    if cliente and cliente.desconto_percentual:
        preco = preco * (1 - cliente.desconto_percentual / 100.0)
    return round(preco, 2)

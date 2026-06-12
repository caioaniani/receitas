"""Vendas B2B da industria.

Encapsula a logica de:
- Criar venda (cabecalho + itens + parcelas) com baixa de EstoqueProducao
- Editar venda ativa (estorna + recria itens + re-baixa)
- Cancelar / reabrir venda (estorna / re-baixa estoque)
- Reverter status de entrega (sem mexer em estoque)
- Receber pagamento (atualiza parcela, calcula saldo)

Estoque sai do EstoqueProducao (industria/freezer). Quando falta saldo,
registra MovEstoqueProducao tipo='venda_b2b_sem_estoque' (igual logica
das vendas Seru) — sai mesmo assim e fica como auditoria.

Estoque baixado eh rastreado pelos proprios MovEstoqueProducao: o saldo
ainda devido pela venda eh `Σ(venda_b2b) − Σ(venda_b2b_estorno)`. Isso
torna cancelar/editar/reabrir corretos e idempotentes mesmo encadeados.
"""

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

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
    """Acha (ou cria zerado) a linha de EstoqueProducao do item.

    `.with_for_update()` no SELECT: trava a linha ate o commit. Sem isso,
    2 admins registrando venda B2B do mesmo item ao mesmo tempo podem
    ler o mesmo saldo (100), cada um decidir 'sobram 80' e 'sobram 70',
    e o segundo commit sobrescrever o primeiro — sai 50 do estoque mas
    o sistema registra 30. Diferente do Seru sync (advisory lock 7723
    serializa workers), B2B vem de UI, sem essa protecao previa."""
    filtro = {
        'receita_id': receita_id,
        'produto_id': produto_id,
    }
    ep = EstoqueProducao.query.filter_by(**filtro).with_for_update().first()
    if not ep:
        ep = EstoqueProducao(**filtro, quantidade=0)
        db.session.add(ep)
        db.session.flush()
    return ep


def _baixar_item(venda, tipo, item_id, qtd, user=None):
    """Baixa EstoqueProducao para 1 item da venda.

    Cesta (Produto com componentes) baixa cada componente; senao baixa o
    item direto. Registra `venda_b2b` pro que saiu e `venda_b2b_sem_estoque`
    pra falta. Toda referencia comeca com `Venda B2B #{id} ` (espaco) — o
    espaco evita que o LIKE de #5 case com #50 no estorno.
    """
    componentes_cesta = []
    produto_obj = None
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
        return

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


def _saldo_baixado_por_ep(venda_id):
    """{estoque_producao_id: saldo ainda baixado} = baixas − estornos da venda.

    O espaco apos o id no LIKE evita casar #5 com #50/#500.
    """
    saldo = defaultdict(int)
    baixas = MovEstoqueProducao.query.filter(
        MovEstoqueProducao.tipo == 'venda_b2b',
        MovEstoqueProducao.referencia.like(f'Venda B2B #{venda_id} %'),
    ).all()
    for m in baixas:
        saldo[m.estoque_producao_id] += (m.quantidade or 0)
    estornos = MovEstoqueProducao.query.filter(
        MovEstoqueProducao.tipo == 'venda_b2b_estorno',
        MovEstoqueProducao.referencia.like(f'Estorno venda B2B #{venda_id} %'),
    ).all()
    for m in estornos:
        saldo[m.estoque_producao_id] -= (m.quantidade or 0)
    return saldo


def _estornar_estoque(venda, user=None, motivo='cancelada'):
    """Devolve ao EstoqueProducao o saldo ainda baixado pela venda.

    Idempotente: depois de estornar o saldo zera, entao chamadas seguintes
    nao fazem nada. Cobre cesta, falta parcial e edicoes encadeadas.
    """
    saldo = _saldo_baixado_por_ep(venda.id)
    for ep_id, qtd in saldo.items():
        if qtd <= 0:
            continue
        ep = EstoqueProducao.query.get(ep_id)
        if not ep:
            continue
        ep.quantidade = (ep.quantidade or 0) + qtd
        db.session.add(MovEstoqueProducao(
            estoque_producao_id=ep.id,
            tipo='venda_b2b_estorno',
            quantidade=qtd,
            referencia=f'Estorno venda B2B #{venda.id} ({motivo})',
            usuario_id=getattr(user, 'id', None),
        ))


def _aplicar_itens(venda, itens, user=None):
    """Cria os VendaB2BItem + baixa estoque de cada item. Retorna total Decimal.

    Ignora linha com qtd<=0, tipo invalido ou sem id (mesma regra do form).
    """
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
        est = (str(it.get('estado') or '').strip().lower() or None)
        if est not in (None, 'backup', 'assado'):
            est = None
        obs = (str(it.get('observacao') or '').strip()[:200] or None)

        vi = VendaB2BItem(
            venda_id=venda.id,
            receita_id=item_id if tipo == 'receita' else None,
            produto_id=item_id if tipo == 'produto' else None,
            quantidade=qtd,
            preco_unitario=preco,
            desconto_percentual=desc,
            estado=est,
            observacao=obs,
        )
        db.session.add(vi)
        total += vi.valor_total
        _baixar_item(venda, tipo, item_id, qtd, user)
    return total


def _aplicar_parcelas(venda, parcelas):
    """Cria as parcelas da venda. Sem parcelas = 1 parcela unica ao total."""
    if not parcelas:
        db.session.add(VendaB2BParcela(
            venda_id=venda.id, numero=1,
            vencimento=venda.data_venda,
            valor=venda.valor_total,
        ))
        return
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


def criar_venda(*, cliente_id=None, cliente_nome=None, data_venda=None,
                data_entrega=None, itens, parcelas=None, observacao=None,
                nf_numero=None, user=None):
    """Cria venda B2B + itens + parcelas + baixa estoque.

    itens: lista de {tipo: 'receita'|'produto', id, quantidade,
                     preco_unitario, desconto_percentual, estado, observacao}
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
        data_entrega=data_entrega,
        cliente_id=cliente_id,
        cliente_nome=(cliente_nome or '').strip() or None,
        observacao=(observacao or '').strip() or None,
        nf_numero=(nf_numero or '').strip() or None,
        criado_por_id=getattr(user, 'id', None),
        valor_total=0,
    )
    db.session.add(venda)
    db.session.flush()

    total = _aplicar_itens(venda, itens, user)
    venda.valor_total = total.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    _aplicar_parcelas(venda, parcelas)
    db.session.commit()
    return venda


def editar_venda(venda, *, cliente_id=None, cliente_nome=None, data_venda=None,
                 data_entrega=None, itens, parcelas=None, observacao=None,
                 nf_numero=None, user=None):
    """Edita uma venda ATIVA por completo: estorna o estoque baixado, apaga
    itens/parcelas antigos, recria com os novos dados e re-baixa.

    Recusa se a venda estiver cancelada (reabra antes) ou se ja houver
    pagamento (protege as contas a receber de inconsistencia). Pra mexer so
    no cabecalho de uma venda paga, use `editar_cabecalho`.
    """
    if venda.status == 'cancelada':
        raise ValueError('venda cancelada — reabra antes de editar')
    if venda.valor_pago and venda.valor_pago > 0:
        raise ValueError('venda com pagamento registrado — itens nao podem ser editados')
    if not itens:
        raise ValueError('venda sem itens')
    if not cliente_id and not (cliente_nome or '').strip():
        raise ValueError('cliente obrigatorio (cadastrado ou avulso)')

    _estornar_estoque(venda, user=user, motivo='edicao')
    for vi in list(venda.itens):
        db.session.delete(vi)
    for p in list(venda.parcelas):
        db.session.delete(p)
    db.session.flush()

    venda.cliente_id = cliente_id
    venda.cliente_nome = (cliente_nome or '').strip() or None
    if data_venda:
        venda.data_venda = data_venda
    venda.data_entrega = data_entrega
    venda.observacao = (observacao or '').strip() or None
    venda.nf_numero = (nf_numero or '').strip() or None

    total = _aplicar_itens(venda, itens, user)
    venda.valor_total = total.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    _aplicar_parcelas(venda, parcelas)
    db.session.commit()
    return venda


def editar_cabecalho(venda, *, cliente_id=None, cliente_nome=None,
                     data_venda=None, data_entrega=None, observacao=None,
                     nf_numero=None):
    """Edita so o cabecalho (cliente, datas, obs, nf) — NAO toca itens, estoque
    nem parcelas. Usado quando a venda ja tem pagamento e os itens estao travados.
    """
    if not cliente_id and not (cliente_nome or '').strip():
        raise ValueError('cliente obrigatorio (cadastrado ou avulso)')
    venda.cliente_id = cliente_id
    venda.cliente_nome = (cliente_nome or '').strip() or None
    if data_venda:
        venda.data_venda = data_venda
    venda.data_entrega = data_entrega
    venda.observacao = (observacao or '').strip() or None
    venda.nf_numero = (nf_numero or '').strip() or None
    db.session.commit()
    return venda


def reabrir_venda(venda, user=None):
    """Venda cancelada volta a 'ativa' e re-baixa o estoque dos itens atuais.

    Idempotente: se a venda nao esta cancelada, nao faz nada.
    """
    if venda.status != 'cancelada':
        return venda
    venda.status = 'ativa'
    venda.cancelado_em = None
    venda.cancelado_por_id = None
    for vi in venda.itens:
        tipo = 'receita' if vi.receita_id else 'produto'
        item_id = vi.receita_id or vi.produto_id
        if not item_id:
            continue
        _baixar_item(venda, tipo, item_id, vi.quantidade, user)
    db.session.commit()
    return venda


_STATUS_ENTREGA_ORDEM = ['pendente', 'separado', 'em_transporte', 'entregue']


def reverter_status_entrega(venda):
    """Volta um passo no status de entrega (entregue→separado→pendente).

    Nao mexe em estoque — o estoque B2B sai na criacao, nao na mudanca de
    status de entrega. Idempotente em 'pendente'.
    """
    cur = venda.status_entrega or 'pendente'
    if cur in _STATUS_ENTREGA_ORDEM and _STATUS_ENTREGA_ORDEM.index(cur) > 0:
        venda.status_entrega = _STATUS_ENTREGA_ORDEM[_STATUS_ENTREGA_ORDEM.index(cur) - 1]
        db.session.commit()
    return venda


def cancelar_venda(venda, user=None):
    """Estorna estoque (por saldo) e marca venda como cancelada. Idempotente."""
    if venda.status == 'cancelada':
        return venda
    _estornar_estoque(venda, user=user, motivo='cancelada')
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
    from decimal import InvalidOperation
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

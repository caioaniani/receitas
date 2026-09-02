"""Fechamento mensal da conta B2B (07/07/2026).

Cliente com `ClienteB2B.faturamento_mensal` compra o mês inteiro (cada
entrega é uma VendaB2B normal, sem parcela). Na virada, `fechar_conta`
agrupa as vendas do período numa `FaturaB2B`:

- cada venda ganha UMA parcela (vencimento = o da fatura, marcada com
  `fatura_id`) — o contas a receber continua funcionando por parcela;
- a fatura emite UMA NF consolidada no Tiny (ver `tiny_nf_b2b.
  emitir_nf_fatura`) e UM boleto Sicredi do total (Cobranca.fatura_id);
- a liquidação do boleto quita a fatura e todas as parcelas juntas.

Dinheiro: Numeric(10,2) + Decimal sempre (regra da casa).
"""
import logging
from decimal import Decimal

from app.extensions import db
from app.models import FaturaB2B, VendaB2B, VendaB2BParcela
from app.utils import agora

logger = logging.getLogger(__name__)


def vendas_para_fechar(cliente_id, data_inicio, data_fim):
    """Vendas do cliente candidatas ao fechamento: ativas, no período, SEM
    fatura e SEM parcela própria (venda que já tem parcela foi negociada à
    parte — cobra pelo caminho normal, não entra na conta do mês)."""
    vendas = (VendaB2B.query
              .filter(VendaB2B.cliente_id == cliente_id,
                      VendaB2B.status == 'ativa',
                      VendaB2B.dispensa_cobranca.is_(None),
                      VendaB2B.fatura_id.is_(None),
                      VendaB2B.data_venda >= data_inicio,
                      VendaB2B.data_venda <= data_fim)
              .order_by(VendaB2B.data_venda.asc(), VendaB2B.id.asc())
              .all())
    return [v for v in vendas if not v.parcelas]


def fechar_conta(cliente, data_inicio, data_fim, vencimento, user_id=None):
    """Fecha a conta do período: cria a FaturaB2B, vincula as vendas e cria
    1 parcela por venda com o vencimento da fatura. Levanta ValueError se
    não houver o que fechar ou datas inválidas."""
    if data_fim < data_inicio:
        raise ValueError('período inválido (fim antes do início).')
    if vencimento < data_fim:
        raise ValueError('vencimento antes do fim do período.')
    vendas = vendas_para_fechar(cliente.id, data_inicio, data_fim)
    if not vendas:
        raise ValueError(
            f'{cliente.nome} não tem venda em aberto (sem parcela e sem '
            f'fatura) entre {data_inicio.strftime("%d/%m")} e '
            f'{data_fim.strftime("%d/%m/%Y")}.')

    # Trava as linhas contra duplo clique: dois POSTs simultâneos leriam o
    # mesmo universo e fechariam a conta DUAS vezes (parcelas dobradas).
    # FOR UPDATE serializa no Postgres (SQLite trava o arquivo inteiro);
    # depois do lock, re-filtra — quem perdeu a corrida não vê mais nada.
    ids = [v.id for v in vendas]
    vendas = (VendaB2B.query.filter(VendaB2B.id.in_(ids))
              .populate_existing().with_for_update().all())
    vendas = [v for v in vendas
              if v.fatura_id is None and v.status == 'ativa'
              and not v.parcelas and not v.sem_cobranca]
    if not vendas:
        raise ValueError('essas vendas acabaram de ser fechadas em outra '
                         'aba/clique — confira a lista de faturas.')

    total = sum((Decimal(v.valor_total or 0) for v in vendas), Decimal('0'))
    fatura = FaturaB2B(cliente_id=cliente.id, data_inicio=data_inicio,
                       data_fim=data_fim, vencimento=vencimento,
                       valor_total=total, criado_por_id=user_id)
    db.session.add(fatura)
    db.session.flush()
    for v in vendas:
        v.fatura_id = fatura.id
        db.session.add(VendaB2BParcela(
            venda_id=v.id, numero=1, fatura_id=fatura.id,
            vencimento=vencimento, valor=Decimal(v.valor_total or 0),
            forma_pagamento='boleto'))
    db.session.commit()
    logger.info('fatura B2B %s fechada: cliente=%s vendas=%d total=%s',
                fatura.codigo, cliente.nome, len(vendas), total)
    return fatura


def cancelar_fatura(fatura, user_id=None):
    """Desfaz o fechamento: apaga as parcelas criadas pela fatura, solta as
    vendas e marca a fatura cancelada. Recusa quando já há dinheiro ou
    documento emitido — aí o caminho é estorno manual, não cancelamento
    silencioso. Levanta ValueError com o motivo."""
    if fatura.status == 'paga':
        raise ValueError('fatura já paga — não cancela.')
    if fatura.status == 'cancelada':
        raise ValueError('fatura já cancelada.')
    if fatura.nf_emitida_em:
        raise ValueError('NF da fatura já emitida na SEFAZ — cancele a '
                         'nota no Tiny antes.')
    # 'pendente' nunca foi ao banco (pode apagar); 'baixada' é título MORTO
    # no banco (não bloqueia — fica pra histórico). O resto (remessa/
    # registrada/paga/rejeitada) exige resolver no banco primeiro.
    cobrancas_vivas = [c for c in fatura.cobrancas
                       if c.status not in ('pendente', 'baixada')]
    if cobrancas_vivas:
        raise ValueError('o boleto da fatura já foi ao banco — baixe o '
                         'título pelo retorno antes de cancelar.')
    pagas = [p for p in fatura.parcelas if (p.valor_pago or 0) > 0]
    if pagas:
        raise ValueError('há parcela da fatura com pagamento registrado — '
                         'estorne antes.')
    for cob in list(fatura.cobrancas):
        if cob.status == 'pendente':
            db.session.delete(cob)
    for p in list(fatura.parcelas):
        db.session.delete(p)
    for v in list(fatura.vendas):
        v.fatura_id = None
    fatura.status = 'cancelada'
    fatura.cancelada_em = agora()
    fatura.cancelada_por_id = user_id
    db.session.commit()
    return fatura


def quitar_fatura(fatura, valor_pago=None, quando=None):
    """Liquidação do boleto da fatura: distribui o valor REALMENTE pago
    pelas parcelas do fechamento (em ordem, até acabar o dinheiro) e marca
    a fatura paga. Sem `valor_pago`, quita pelo valor cheio.

    Dinheiro tem peso especial (CLAUDE.md): pagamento divergente do total
    NÃO é silenciado — as parcelas registram o que entrou de fato (a
    última pode ficar parcial) e a função devolve um AVISO (str) pro
    caller mostrar; None quando bateu certinho.

    NÃO commita — o caller (processar_retorno, que aplica o arquivo
    inteiro numa transação) fecha. Idempotente: parcela já quitada não
    re-quita nem consome o rateio."""
    quando = quando or agora()
    total_aberto = sum((Decimal(p.valor or 0) - Decimal(p.valor_pago or 0)
                        for p in fatura.parcelas if not p.pago_em),
                       Decimal('0'))
    pago = (Decimal(str(valor_pago)) if valor_pago is not None
            else total_aberto)
    restante = pago
    for p in sorted(fatura.parcelas, key=lambda x: x.id):
        if p.pago_em:
            continue
        falta = Decimal(p.valor or 0) - Decimal(p.valor_pago or 0)
        aplicar = min(falta, max(restante, Decimal('0')))
        if aplicar > 0:
            p.valor_pago = Decimal(p.valor_pago or 0) + aplicar
            restante -= aplicar
            p.forma_pagamento = 'boleto'
        if Decimal(p.valor_pago or 0) >= Decimal(p.valor or 0):
            p.pago_em = quando
    fatura.status = 'paga'
    fatura.pago_em = quando
    if pago != total_aberto:
        return (f'fatura {fatura.codigo}: banco liquidou R$ {pago} mas o '
                f'saldo em aberto era R$ {total_aberto} — confira as '
                'parcelas (rateio em ordem; diferença NÃO foi escondida).')
    return None


def itens_consolidados(fatura):
    """Consolida os itens de todas as vendas da fatura pra NF única:
    agrupa por (kind, item_id, preço unitário efetivo) somando quantidades
    — o mesmo pão vendido a preços diferentes no mês vira linhas separadas
    (a NF precisa refletir o que foi cobrado). Preço efetivo = unitário com
    o desconto do item aplicado, em Decimal, QUANTIZADO a 2 casas (mesma
    precisão do dinheiro cobrado; sem isso a NF podia divergir do boleto
    em centavos quando o desconto gera dízima)."""
    from decimal import ROUND_HALF_UP
    grupos = {}
    for v in fatura.vendas:
        for it in v.itens:
            kind = 'receita' if it.receita_id else 'produto'
            preco = Decimal(it.preco_unitario or 0)
            desc = Decimal(str(it.desconto_percentual or 0))
            unitario = (preco * (Decimal('1') - desc / Decimal('100'))
                        ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            chave = (kind, it.receita_id or it.produto_id, unitario)
            g = grupos.setdefault(chave, {
                'kind': kind, 'item_id': it.receita_id or it.produto_id,
                'nome': it.nome_item, 'valor_unitario': unitario,
                'quantidade': 0})
            g['quantidade'] += int(it.quantidade or 0)
    return [g for g in grupos.values() if g['quantidade'] > 0]

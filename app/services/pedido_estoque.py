"""Baixa e estorno de estoque da INDÚSTRIA para pedidos loja→indústria.

Motor ÚNICO (03/07/2026 — auditoria das baixas): a rota web
(`/pedidos/<id>/enviar` + handshake QR) e o copilot (`mudar_status_pedido`)
usam ESTAS funções. Antes o copilot tinha uma cópia inline que divergia, e
AMBAS pulavam em silêncio o item sem linha de `EstoqueProducao` (`if ep:`) —
o pedido saía com baixa zero e nenhum rastro.

Regras canônicas:
- `baixar_industria_pedido`: para CADA item, decrementa `EstoqueProducao`
  criando a linha se não existir (`obter_linha_producao`, get-or-create) e
  grava `MovEstoqueProducao` com a quantidade REALMENTE baixada; o que faltou
  vira mov `saida_pedido_sem_estoque` (mesmo padrão do
  `venda_b2b_sem_estoque`: registra a falta sem deixar saldo negativo).
  Matéria-prima: baixa real com a falta anotada na referência
  (`MovimentacaoEstoque` só tem entrada/saida).
- `estornar_industria_pedido`: espelho EXATO da baixa — devolve o que os
  movimentos do pedido dizem que REALMENTE saiu (`saida_pedido` menos
  `estorno_saida_pedido` anteriores), não a quantidade nominal do item.
  Assim baixa saturada em 0 não vira estoque fantasma no estorno, e
  reenviar depois de um estorno não corrompe a conta (o saldo líquido dos
  movimentos é sempre a verdade).

NÃO commitam nem mexem em status — o caller controla a transação.
"""
import logging

from sqlalchemy import func

from app.extensions import db
from app.models import (
    EstoqueProducao,
    MateriaPrima,
    MovEstoqueProducao,
    MovimentacaoEstoque,
)

logger = logging.getLogger(__name__)


def _ref_base(pedido, ref_extra=None):
    ref = f'Pedido #{pedido.id} → {pedido.loja.nome}'
    if ref_extra:
        ref += f' ({ref_extra})'
    return ref


def baixar_industria_pedido(pedido, usuario_id, ref_extra=None):
    """Baixa EstoqueProducao + MP de todos os itens do pedido.

    Retorna lista de faltas [{'item', 'pedido', 'baixado', 'faltou'}] —
    vazia quando tudo saiu com saldo. Falta NUNCA bloqueia o envio (o
    caminhão sai mesmo; a falta fica registrada pra acerto de inventário).
    """
    from app.services.estoque_congelados import obter_linha_producao

    ref = _ref_base(pedido, ref_extra)
    faltas = []
    for item in pedido.itens:
        qtd = item.quantidade or 0
        if qtd <= 0:
            continue
        if item.materia_prima_id:
            mp = db.session.get(MateriaPrima, item.materia_prima_id)
            if mp is None:
                continue
            disp = float(mp.estoque_atual or 0)
            baixa = min(float(qtd), disp)
            falta = float(qtd) - baixa
            mp.estoque_atual = disp - baixa
            ref_mp = ref + (f' — faltaram {falta:g}' if falta > 0 else '')
            db.session.add(MovimentacaoEstoque(
                materia_prima_id=mp.id, tipo='saida', quantidade=baixa,
                referencia=ref_mp, usuario_id=usuario_id))
            if falta > 0:
                faltas.append({'item': mp.nome, 'pedido': float(qtd),
                               'baixado': baixa, 'faltou': falta})
            continue
        if not (item.receita_id or item.produto_id):
            # Item solto (legado, só nome) — não há linha possível.
            logger.warning('baixar_industria_pedido: item #%s do pedido #%s '
                           'sem FK (receita/produto/MP) — sem baixa', item.id,
                           pedido.id)
            continue
        ep = obter_linha_producao(receita_id=item.receita_id,
                                  produto_id=item.produto_id,
                                  usuario_id=usuario_id)
        disp = int(ep.quantidade or 0)
        baixa = min(int(qtd), disp)
        falta = int(qtd) - baixa
        ep.quantidade = disp - baixa
        if baixa > 0:
            db.session.add(MovEstoqueProducao(
                estoque_producao_id=ep.id, tipo='saida_pedido',
                quantidade=baixa, referencia=ref, usuario_id=usuario_id))
        if falta > 0:
            db.session.add(MovEstoqueProducao(
                estoque_producao_id=ep.id, tipo='saida_pedido_sem_estoque',
                quantidade=falta, referencia=ref, usuario_id=usuario_id))
            faltas.append({'item': ep.nome_item, 'pedido': int(qtd),
                           'baixado': baixa, 'faltou': falta})
    return faltas


def estornar_industria_pedido(pedido, usuario_id, motivo='voltar status'):
    """Devolve à indústria o que o pedido REALMENTE baixou (pelos movimentos),
    linha a linha. Registra `estorno_saida_pedido` — um novo envio depois do
    estorno soma movimentos novos e o líquido continua correto.

    Retorna o total de unidades devolvidas (EstoqueProducao)."""
    ref_like = f'Pedido #{pedido.id} →%'
    ref_estorno = f'Estorno pedido #{pedido.id} ({motivo})'

    # EstoqueProducao: líquido por linha = saida_pedido − estornos anteriores.
    saidas = dict(db.session.query(
        MovEstoqueProducao.estoque_producao_id,
        func.sum(MovEstoqueProducao.quantidade))
        .filter(MovEstoqueProducao.tipo == 'saida_pedido',
                MovEstoqueProducao.referencia.like(ref_like))
        .group_by(MovEstoqueProducao.estoque_producao_id).all())
    # 'ajuste' entra por compat: era o tipo do estorno ANTES deste motor —
    # sem ele, pedido estornado no código antigo e re-estornado aqui
    # devolveria em dobro.
    estornos = dict(db.session.query(
        MovEstoqueProducao.estoque_producao_id,
        func.sum(MovEstoqueProducao.quantidade))
        .filter(MovEstoqueProducao.tipo.in_(('estorno_saida_pedido', 'ajuste')),
                MovEstoqueProducao.referencia.like(f'Estorno pedido #{pedido.id} %'))
        .group_by(MovEstoqueProducao.estoque_producao_id).all())
    devolvidas = 0
    for ep_id, total in saidas.items():
        liquido = int(total or 0) - int(estornos.get(ep_id, 0) or 0)
        if liquido <= 0:
            continue
        ep = db.session.get(EstoqueProducao, ep_id)
        if ep is None:
            continue
        ep.quantidade = (ep.quantidade or 0) + liquido
        db.session.add(MovEstoqueProducao(
            estoque_producao_id=ep_id, tipo='estorno_saida_pedido',
            quantidade=liquido, referencia=ref_estorno,
            usuario_id=usuario_id))
        devolvidas += liquido

    # MP: líquido = saidas do pedido − entradas de estorno anteriores.
    saidas_mp = dict(db.session.query(
        MovimentacaoEstoque.materia_prima_id,
        func.sum(MovimentacaoEstoque.quantidade))
        .filter(MovimentacaoEstoque.tipo == 'saida',
                MovimentacaoEstoque.referencia.like(ref_like))
        .group_by(MovimentacaoEstoque.materia_prima_id).all())
    estornos_mp = dict(db.session.query(
        MovimentacaoEstoque.materia_prima_id,
        func.sum(MovimentacaoEstoque.quantidade))
        .filter(MovimentacaoEstoque.tipo == 'entrada',
                MovimentacaoEstoque.referencia.like(f'Estorno pedido #{pedido.id} %'))
        .group_by(MovimentacaoEstoque.materia_prima_id).all())
    for mp_id, total in saidas_mp.items():
        liquido = float(total or 0) - float(estornos_mp.get(mp_id, 0) or 0)
        if liquido <= 0:
            continue
        mp = db.session.get(MateriaPrima, mp_id)
        if mp is None:
            continue
        mp.estoque_atual = (mp.estoque_atual or 0) + liquido
        db.session.add(MovimentacaoEstoque(
            materia_prima_id=mp_id, tipo='entrada', quantidade=liquido,
            referencia=ref_estorno, usuario_id=usuario_id))
    return devolvidas

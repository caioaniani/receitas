"""Itens que a indústria NÃO fornece dentro de pedidos loja→indústria futuros
(dono 01/10/2026 — "os pedidos das lojas para a indústria devem ser somente
do que a indústria produz, está vindo de tudo até coca cola e adicional de
morango").

Caso real: o motor venda+estoque sugeriu Produto de revenda (água, sucos,
adicionais) nos rascunhos automáticos e as lojas confirmaram. O motor
deixou de sugerir Produto; os rascunhos ainda PENDENTES do próprio cron se
corrigem sozinhos na re-sincronização (item fora da grade vai a zero). Os
pedidos já CONFIRMADOS pela loja são protegidos do cron — a limpeza deles
é gesto do dono (`GET /admin/pedidos-revenda?executar=1`); a sonda
`/api/claude/pedidos-revenda` mostra a mesma lista sem tocar em nada.

Fonte única da régua: `app/services/itens_industria.py`.
"""
from sqlalchemy.orm import joinedload

from app.constants import STATUS_PEDIDO_EDITAVEIS
from app.extensions import db
from app.models import PedidoItem, PedidoLoja, Usuario
from app.services.itens_industria import (
    motivo_produto_nao_fornecido,
    rotulo_motivo,
)
from app.services.pedido_corte import corte_ativo
from app.utils import agora, hoje


def listar(hoje_d=None):
    """Linhas (uma por PedidoItem) de Produto não fornecido em pedido
    pendente/confirmado com entrega DEPOIS de `hoje_d`. Cada linha traz
    `sob_corte` (pedido de amanhã já fechado pelo corte das 12h — a limpeza
    não o toca) e `_item` (o PedidoItem, para `remover`)."""
    hoje_d = hoje_d or hoje()

    from app.services.itens_industria import opcoes_composicao
    prod = joinedload(PedidoItem.produto)
    rows = (PedidoItem.query.join(PedidoLoja)
            .options(joinedload(PedidoItem.pedido).joinedload(PedidoLoja.loja),
                     *[prod.options(o) for o in opcoes_composicao()])
            .filter(PedidoItem.produto_id.isnot(None),
                    PedidoLoja.status.in_(STATUS_PEDIDO_EDITAVEIS),
                    PedidoLoja.data_entrega > hoje_d)
            .order_by(PedidoLoja.data_entrega, PedidoLoja.id, PedidoItem.id)
            .all())
    usuarios = None
    linhas = []
    for it in rows:
        motivo = motivo_produto_nao_fornecido(it.produto)
        if motivo is None:
            continue
        if usuarios is None:
            usuarios = {u.id: u.nome for u in Usuario.query.all()}
        p = it.pedido
        linhas.append({
            'pedido_id': p.id,
            'loja': p.loja.nome if p.loja else p.loja_id,
            'data_entrega': p.data_entrega.isoformat(),
            'status': p.status,
            'criado_por': usuarios.get(p.criado_por) or 'automático',
            'modificado_por_humano': p.modificado_por_id is not None,
            'item': it.nome_item,
            'quantidade': it.quantidade,
            'motivo': rotulo_motivo(motivo),
            'sob_corte': corte_ativo(p.data_entrega),
            '_item': it,
        })
    return linhas


def publicas(linhas):
    """As linhas sem o objeto interno — para JSON."""
    return [{k: v for k, v in li.items() if not k.startswith('_')} for li in linhas]


def remover(linhas):
    """Apaga os PedidoItem das linhas que NÃO estão sob o corte, carimba
    `modificado_em` (nunca `modificado_por_id`: rascunho do cron segue
    re-sincronizável; carimbo humano existente fica) e cancela o pedido que
    ficar sem item. O pedido cancelado por ficar vazio tem o carimbo humano
    LIMPO de propósito: cancelado com carimbo é "palavra da loja" e protege
    o dia contra o cron (`pedido_lock.protegido_do_motor`) — aqui o pedido
    nunca teve nada a entregar, então a loja deve voltar a receber o
    rascunho automático do dia (achado da revisão de 01/10/2026). NÃO
    commita — o chamador decide. Devolve (removidos, ids_dos_pedidos_
    cancelados, datas_tocadas) — as datas servem para reconferir o corte
    antes de gravar."""
    pedidos = {}
    removidos = 0
    for li in linhas:
        if li['sob_corte']:
            continue
        it = li['_item']
        pedidos[it.pedido_id] = it.pedido
        db.session.delete(it)
        removidos += 1
    db.session.flush()
    cancelados = []
    for p in pedidos.values():
        db.session.expire(p, ['itens'])
        p.modificado_em = agora()
        if not p.itens:
            p.status = 'cancelado'
            p.modificado_por_id = None
            cancelados.append(p.id)
    datas = sorted({p.data_entrega for p in pedidos.values() if p.data_entrega})
    return removidos, cancelados, datas

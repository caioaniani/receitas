"""Minis fora do pedido loja→indústria (dono, 07/10/2026).

Foto da tela de separação: "2× Mini Danish de Calabresa" no pedido de loja
— "O robô ainda continua pedindo mini como pedido da loja fazendo para a
indústria, não pode". Causa provada pela sonda `/api/claude/pedidos-itens`
(2 por loja por dia, todos `criado_por` None = rascunho do cron): o seed
`_seed_piso_dinamico_especiais` (04/09/2026) pôs `pedido_minimo_diario = 2`
em TODA receita com "danish" no nome, minis inclusive, e o Mini Danish de
Calabresa era o único mini de danish ainda com `sugerir_pedido_loja=True`
(os outros já estavam desmarcados na ficha).

Gesto canônico = a flag da ficha "Insumo/etapa — a loja não pede direto"
(`Receita.sugerir_pedido_loja = False`): o motor venda+estoque só considera
receitas com a flag ligada, então piso, mínimo e venda deixam de gerar
pedido. Aplicado a toda receita ativa cujo nome começa com "Mini". O piso e
o mínimo das linhas de estoque dessas receitas são zerados (não servem para
nada fora da reposição). Itens de mini já lançados em pedido pendente/
confirmado com entrega futura são removidos pelo mesmo `remover` da limpeza
de revenda (respeita o corte das 12h; pedido que fica vazio é cancelado).
Sem commit — quem chama decide.
"""
from sqlalchemy.orm import joinedload

from app.constants import STATUS_PEDIDO_EDITAVEIS
from app.extensions import db
from app.models import EstoqueLoja, PedidoItem, PedidoLoja, Receita
from app.services.pedido_corte import corte_ativo
from app.utils import normalizar_busca


def eh_mini(receita):
    return normalizar_busca(receita.nome or '').strip().startswith('mini ')


def minis_ativos():
    return [r for r in Receita.ativas().all() if eh_mini(r)]


def desmarcar_minis():
    """Flag 'a loja não pede' + zera piso/mínimo de loja dos minis."""
    resumo = {'desmarcados': [], 'pisos_zerados': 0}
    minis = minis_ativos()
    for r in minis:
        if r.sugerir_pedido_loja is not False:
            r.sugerir_pedido_loja = False
            resumo['desmarcados'].append(r.nome)
    ids = [r.id for r in minis]
    if ids:
        for el in EstoqueLoja.query.filter(EstoqueLoja.receita_id.in_(ids)).all():
            if el.pedido_minimo_diario or el.estoque_minimo:
                el.pedido_minimo_diario = None
                el.estoque_minimo = 0
                resumo['pisos_zerados'] += 1
    db.session.flush()
    return resumo


def itens_em_pedidos_futuros(hoje_d):
    """Linhas no formato de `pedidos_revenda.remover` (com `_item` e
    `sob_corte`)."""
    ids = [r.id for r in minis_ativos()]
    if not ids:
        return []
    rows = (PedidoItem.query.join(PedidoLoja)
            .options(joinedload(PedidoItem.pedido).joinedload(PedidoLoja.loja))
            .filter(PedidoItem.receita_id.in_(ids),
                    PedidoLoja.status.in_(STATUS_PEDIDO_EDITAVEIS),
                    PedidoLoja.data_entrega > hoje_d)
            .order_by(PedidoLoja.data_entrega, PedidoLoja.id).all())
    return [{'pedido_id': it.pedido_id,
             'loja': it.pedido.loja.nome if it.pedido.loja else it.pedido.loja_id,
             'data_entrega': it.pedido.data_entrega.isoformat(),
             'item': it.nome_item, 'quantidade': it.quantidade,
             'sob_corte': corte_ativo(it.pedido.data_entrega), '_item': it}
            for it in rows]

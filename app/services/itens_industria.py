"""O que a loja pode pedir à indústria — regra canônica (dono, 01/10/2026).

Pedido do dono: "os pedidos das lojas para a indústria devem ser somente do
que a indústria produz, está vindo de tudo, até Coca-Cola e adicional de
morango". Caso real: o motor venda+estoque passou a sugerir qualquer item
com histórico de venda na loja, e Produtos de revenda (água, sucos,
adicionais) entraram nos rascunhos automáticos, que as lojas confirmaram.

Uma única régua, usada por TODAS as camadas (motor venda+estoque, grades
da semana, typeahead, POST novo/editar, copilot, limpeza dos pedidos):

- Receita ativa: a indústria PRODUZ. Entra.
- Matéria-prima: só a liberada no checkbox "sugerir pedido loja" do Banco
  de MPs (decisão do dono 07/07/2026 — a indústria compra e ENVIA, ex.
  pão de queijo congelado). Essa regra não mudou.
- Produto: só CESTA (tem composição) em que pelo menos um componente é
  produzido (receita) ou enviado (MP liberada) pela indústria — direto ou
  por um produto-componente que também seja fornecido. Produto SEM
  composição é revenda comprada pronta (água, refrigerante, adicional,
  suco de máquina): NUNCA entra num pedido loja→indústria.

Item que JÁ está num pedido continua válido na edição (grandfather, mesmo
padrão das MPs): a trava vale para item NOVO. Nada aqui mexe em estoque,
venda ou composição — é só quem pode entrar no pedido.
"""

MOTIVO_INATIVO = 'inativo'
MOTIVO_REVENDA = 'revenda'
MOTIVO_CESTA_DE_REVENDA = 'cesta_de_revenda'

_ROTULO_MOTIVO = {
    MOTIVO_INATIVO: 'produto inativo',
    MOTIVO_REVENDA: 'produto de revenda, a indústria não produz',
    MOTIVO_CESTA_DE_REVENDA: ('cesta sem componente que a indústria produza ou '
                              'envie: nenhuma receita ativa nem MP liberada no '
                              'Banco de MPs; componente órfão não conta, '
                              'resolva em Cestas → órfãos'),
}

ORIENTACAO_PEDIDO = ('O pedido para a indústria aceita receitas, cestas '
                     'montadas pela indústria e matérias-primas liberadas '
                     'no Banco de MPs.')


def opcoes_composicao():
    """Opções de carregamento para avaliar a régua sem N+1: a composição
    (ProdutoItem) e os alvos de cada componente numa ida só. Usar em toda
    query de Produto cujo resultado passa por `motivo_produto_nao_fornecido`
    em loop (typeahead, resolver do copilot, sugestão por loja, limpeza)."""
    from sqlalchemy.orm import selectinload

    from app.models import Produto, ProdutoItem
    comp = selectinload(Produto.itens)
    return [comp.joinedload(ProdutoItem.receita),
            comp.joinedload(ProdutoItem.materia_prima),
            comp.joinedload(ProdutoItem.produto_componente)]


def mp_fornecida_pela_industria(mp):
    """MP que a loja pode pedir: ativa e liberada no checkbox (opt-in)."""
    return bool(mp and mp.arquivada_em is None and mp.sugerir_pedido_loja)


def motivo_produto_nao_fornecido(produto, _visitados=None):
    """None quando a indústria fornece o Produto; senão o motivo
    (MOTIVO_INATIVO / MOTIVO_REVENDA / MOTIVO_CESTA_DE_REVENDA)."""
    if produto is None or not produto.ativo:
        return MOTIVO_INATIVO
    itens = list(produto.itens or [])
    if not itens:
        return MOTIVO_REVENDA
    visitados = set(_visitados or ())
    visitados.add(produto.id)
    for pi in itens:
        if pi.tipo == 'receita' and pi.receita_id and pi.receita is not None:
            if pi.receita.arquivada_em is None:
                return None
        elif pi.tipo == 'mp' and pi.materia_prima_id:
            if mp_fornecida_pela_industria(pi.materia_prima):
                return None
        elif pi.tipo == 'produto' and pi.produto_componente_id:
            comp = pi.produto_componente
            if (comp is not None and comp.id not in visitados
                    and motivo_produto_nao_fornecido(comp, visitados) is None):
                return None
    return MOTIVO_CESTA_DE_REVENDA


def produto_fornecido_pela_industria(produto):
    """Produto que a loja pode pedir à indústria (ver docstring do módulo)."""
    return motivo_produto_nao_fornecido(produto) is None


def rotulo_motivo(motivo):
    return _ROTULO_MOTIVO.get(motivo, motivo or '')


def produtos_nao_pediveis(itens_norm, produto_ids_extras=()):
    """Produtos de `itens_norm` (dicts com `produto_id`) que a indústria NÃO
    fornece — [{'id', 'nome', 'motivo'}]. Lista vazia = tudo certo.

    `produto_ids_extras`: ids liberados por exceção (grandfather do editar —
    produto que JÁ está no pedido não derruba a edição)."""
    from app.models import Produto

    extras = set(produto_ids_extras or ())
    ids = sorted({int(it['produto_id']) for it in itens_norm
                  if it.get('produto_id') and int(it['produto_id']) not in extras})
    if not ids:
        return []
    produtos = {p.id: p for p in Produto.query.filter(Produto.id.in_(ids))
                .options(*opcoes_composicao()).all()}
    bloqueados = []
    for pid in ids:
        p = produtos.get(pid)
        motivo = motivo_produto_nao_fornecido(p)
        if motivo is not None:
            bloqueados.append({'id': pid,
                               'nome': p.nome if p is not None else f'produto #{pid}',
                               'motivo': motivo})
    return bloqueados


def mensagem_produtos_nao_pediveis(bloqueados):
    """Texto único das telas e do copilot quando `produtos_nao_pediveis`
    devolve algo."""
    partes = [f"{b['nome']} ({rotulo_motivo(b['motivo'])})" for b in bloqueados]
    return ('Item(ns) que a indústria não produz nem envia: '
            + ', '.join(partes) + '. ' + ORIENTACAO_PEDIDO)

"""Reajuste de precos em MASSA, em REAIS (02/07/2026, pedido do dono).

Regra (decisao do dono, refinada em 02/07 apos a 1a previa contar GRAMAS
como unidades — croissant de nutella com "100g de nutella" saia como 101
unidades e +R$ 204):
- item AVULSO (receita ou produto simples) com preco cadastrado: + valor;
- CESTA/KIT de verdade: + valor FIXO + valor x unidades dentro, onde
  componente vendido POR UNIDADE (receita/produto/MP 'un') conta pela
  quantidade e componente em PESO/VOLUME (g/ml/kg/l — frios, recheios)
  conta como 1 PORCAO por linha ("porcao = 1 produto", decisao do dono);
- COMPOSTO DE ITEM UNICO (<= 1 unidade vendavel: croissant de nutella =
  1 croissant + recheio; Mussarela 100g = so a porcao; Mel 40g): a
  composicao e tecnica (baixa de estoque) — trata como AVULSO, + valor;
- item SEM o preco cadastrado (NULL) fica INTOCADO — reajuste nunca inventa
  preco.

Fluxo em 2 passos na tela /receitas/precos: "Pre-visualizar" mostra a tabela
atual -> novo item a item; "Aplicar" recalcula DO ESTADO ATUAL e grava (a
previa e informativa; se um preco mudar entre os dois cliques, vale o estado
na hora do aplicar).

`campo` aceita: preco_site (o caso pedido), preco_loja, preco_interno,
preco_atacado. No atacado, o campo da RECEITA chama `preco_venda` (historico)
e o do PRODUTO `preco_atacado` — o mapeamento fica aqui, num lugar so.
"""
from app.models import Produto, Receita

# campo logico -> (atributo na Receita, atributo no Produto)
CAMPOS_REAJUSTE = {
    'preco_site': ('preco_site', 'preco_site'),
    'preco_loja': ('preco_loja', 'preco_loja'),
    'preco_interno': ('preco_interno', 'preco_interno'),
    'preco_atacado': ('preco_venda', 'preco_atacado'),
}

CAMPO_LABEL = {
    'preco_site': 'Site',
    'preco_loja': 'Loja',
    'preco_interno': 'Interno',
    'preco_atacado': 'Atacado',
}

_UNIDADES_PESO_VOLUME = {'g', 'ml', 'kg', 'l'}


def _unidades_cesta(produto):
    """(vendaveis, porcoes) dos componentes da cesta:
    - vendaveis: soma das quantidades dos componentes vendidos por UNIDADE
      (receita/produto/MP com unidade 'un') — 2 croissants = 2;
    - porcoes: nº de linhas em PESO/VOLUME (100g de mussarela = 1 porcao,
      nao 100). Orfao de MP sem unidade conhecida cai em porcao (1)."""
    vendaveis = 0.0
    porcoes = 0
    for pi in produto.itens:
        if pi.tipo == 'mp' and pi.materia_prima is None:
            porcoes += 1          # orfao de MP: unidade desconhecida = porcao
        elif pi.unidade_resolvida in _UNIDADES_PESO_VOLUME:
            porcoes += 1
        else:
            vendaveis += float(pi.quantidade or 0)
    return vendaveis, porcoes


def previa_reajuste(campo, valor):
    """Monta a previa do reajuste: lista de linhas
    {tipo, id, nome, unidades, preco_atual, aumento, preco_novo} para todos
    os itens COM o preco cadastrado, e a contagem dos pulados (sem preco).
    Nao grava nada."""
    if campo not in CAMPOS_REAJUSTE:
        raise ValueError(f'campo invalido: {campo}')
    valor = round(float(valor), 2)
    attr_receita, attr_produto = CAMPOS_REAJUSTE[campo]

    linhas = []
    pulados = 0
    for r in (Receita.query.filter(Receita.arquivada_em.is_(None))
              .order_by(Receita.categoria, Receita.nome).all()):
        atual = getattr(r, attr_receita)
        if atual is None:
            pulados += 1
            continue
        linhas.append({'tipo': 'receita', 'id': r.id, 'nome': r.nome,
                       'unidades': None,
                       'preco_atual': round(float(atual), 2),
                       'aumento': valor,
                       'preco_novo': round(float(atual) + valor, 2)})
    for p in (Produto.query.filter_by(ativo=True)
              .order_by(Produto.categoria, Produto.nome).all()):
        atual = getattr(p, attr_produto)
        if atual is None:
            pulados += 1
            continue
        vendaveis, porcoes = _unidades_cesta(p)
        if not p.itens:                       # produto simples
            tipo, unidades, aumento = 'produto', None, valor
        elif vendaveis <= 1:
            # Composto de item unico (croissant de nutella = 1 croissant +
            # recheio; Mussarela 100g = so a porcao): composicao e tecnica,
            # sobe como avulso (decisao do dono 02/07).
            tipo, unidades, aumento = 'composto', None, valor
        else:                                 # cesta/kit de verdade
            unidades = vendaveis + porcoes
            tipo = 'cesta'
            aumento = round(valor + valor * unidades, 2)
        linhas.append({'tipo': tipo,
                       'id': p.id, 'nome': p.nome,
                       'unidades': unidades,
                       'preco_atual': round(float(atual), 2),
                       'aumento': aumento,
                       'preco_novo': round(float(atual) + aumento, 2)})
    return {'campo': campo, 'valor': valor, 'linhas': linhas,
            'pulados_sem_preco': pulados,
            'total_itens': len(linhas)}


def aplicar_reajuste(campo, valor):
    """Aplica o reajuste (mesma conta da previa, recalculada do estado atual)
    e retorna a contagem de itens alterados. O COMMIT e do chamador — a rota
    decide a transacao."""
    previa = previa_reajuste(campo, valor)
    attr_receita, attr_produto = CAMPOS_REAJUSTE[campo]
    por_chave = {(ln['tipo'], ln['id']): ln for ln in previa['linhas']}

    alterados = 0
    for r in Receita.query.filter(Receita.arquivada_em.is_(None)).all():
        ln = por_chave.get(('receita', r.id))
        if ln is not None:
            setattr(r, attr_receita, ln['preco_novo'])
            alterados += 1
    for p in Produto.query.filter_by(ativo=True).all():
        ln = (por_chave.get(('cesta', p.id))
              or por_chave.get(('composto', p.id))
              or por_chave.get(('produto', p.id)))
        if ln is not None:
            setattr(p, attr_produto, ln['preco_novo'])
            alterados += 1
    return alterados

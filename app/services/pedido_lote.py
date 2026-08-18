"""Restricao de LOTE pra item de pedido medido em g/ml (dono 18/08/2026).

Caso real (auditoria "granola/iogurte em POTES x gramas"): itens a granel
como "Produção - Granola Artesanal 1000g" sao medidos em gramas, mas as
lojas lancavam a quantidade em POTES (3, 5, 15) — relatorio ~1000x
distorcido e demanda irreal. Decisao do dono via AskUserQuestion:
"Múltiplos do padrão" — o pedido desses itens SO aceita multiplos do
`lote_pedido` da receita (iogurte 3000 ml, granola 5000 g; seed
`acerto_granola_iogurte_2026_08`).

Escopo DELIBERADO: so receita com `medida_em_gramas` E `lote_pedido`
definido. Receita em unidades com lote_pedido (croissant lote 50) segue
como sempre — la o lote so arredonda a SUGESTAO, a loja pode pedir 45 na
mao. NAO alargar sem ordem. Consequencia aceita pelo dono: o 9360 que
Anesio/Ribeiro pediam de iogurte deixa de valer (vira 9000 ou 12000).

Defesa em profundidade (padrao da trava de MP pedivel): web novo + editar
e executores do copilot — POST direto/preview re-enviado nao fura.
"""


def violacoes_de_lote(pares):
    """Valida pares (receita, quantidade). Devolve lista de mensagens
    legiveis (vazia = tudo certo). `receita` None e ignorado (produto/MP
    ficam fora da regra)."""
    erros = []
    for rec, qtd in pares:
        if rec is None:
            continue
        lote = int(getattr(rec, 'lote_pedido', 0) or 0)
        if lote <= 0 or not getattr(rec, 'medida_em_gramas', False):
            continue
        try:
            q = int(qtd)
        except (TypeError, ValueError):
            continue
        if q > 0 and q % lote != 0:
            erros.append(
                f'{rec.nome}: a quantidade deve ser múltiplo de {lote} '
                f'(item medido em g/ml — o padrão é {lote}). '
                f'Você pediu {q}.')
    return erros


def violacoes_por_ids(itens_norm):
    """Mesma regra a partir de itens normalizados do form
    ({'receita_id': ..., 'quantidade': ...}) — resolve as receitas em UMA
    query."""
    from app.models import Receita
    rec_ids = [it['receita_id'] for it in itens_norm if it.get('receita_id')]
    if not rec_ids:
        return []
    por_id = {r.id: r for r in Receita.query
              .filter(Receita.id.in_(rec_ids)).all()}
    pares = [(por_id.get(it['receita_id']), it.get('quantidade'))
             for it in itens_norm if it.get('receita_id')]
    return violacoes_de_lote(pares)

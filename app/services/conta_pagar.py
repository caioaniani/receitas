"""Logica de dominio de Contas a Pagar.

Agrupamento de NF + boleto do mesmo recebimento. Criterio DETERMINISTICO e
conservador (dinheiro tem peso): so junta documentos da mesma loja
(origem_canal), com o MESMO valor_total e o MESMO vencimento — todos os tres
preenchidos. Assim nao corre risco de esconder uma conta a pagar juntando
recebimentos diferentes.

Modelo do grupo: um documento "principal" (relacionado_id IS NULL) e os demais
apontam pra ele via relacionado_id. A lista mostra so os principais; o detalhe
mostra o par (property `ligados`, bidirecional).
"""
from app.extensions import db
from app.models import ContaPagar


def _agrupavel(c):
    return (c.origem_canal and c.valor_total is not None
            and c.vencimento is not None and c.status != 'ignorado')


def tentar_agrupar(conta):
    """Liga `conta` a um documento ja existente do mesmo recebimento.
    Retorna True se agrupou. Usar logo apos criar uma conta nova."""
    if conta.relacionado_id is not None or not _agrupavel(conta):
        return False
    principal = (ContaPagar.query
                 .filter(ContaPagar.id != conta.id,
                         ContaPagar.origem_canal == conta.origem_canal,
                         ContaPagar.valor_total == conta.valor_total,
                         ContaPagar.vencimento == conta.vencimento,
                         ContaPagar.status != 'ignorado',
                         ContaPagar.relacionado_id.is_(None))
                 .order_by(ContaPagar.id.asc()).first())
    if principal is None:
        return False
    conta.relacionado_id = principal.id
    db.session.commit()
    return True


def agrupar_automatico():
    """Varre todas as contas e junta os recebimentos (retroativo).
    Idempotente. Retorna nº de documentos que foram agrupados nesta passada."""
    from collections import defaultdict

    contas = (ContaPagar.query
              .filter(ContaPagar.status != 'ignorado',
                      ContaPagar.valor_total.isnot(None),
                      ContaPagar.vencimento.isnot(None))
              .order_by(ContaPagar.id.asc()).all())
    buckets = defaultdict(list)
    for c in contas:
        buckets[(c.origem_canal, str(c.valor_total), c.vencimento)].append(c)

    n = 0
    for grupo in buckets.values():
        if len(grupo) < 2:
            continue
        principal = next((c for c in grupo if c.relacionado_id is None), None)
        if principal is None:
            continue
        for c in grupo:
            if c.id != principal.id and c.relacionado_id is None:
                c.relacionado_id = principal.id
                n += 1
    db.session.commit()
    return n

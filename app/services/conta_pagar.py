"""Logica de dominio de Contas a Pagar.

Agrupamento de NF + boleto do mesmo recebimento. Cada documento ganha UMA chave
deterministica e conservadora (dinheiro tem peso); documentos com a mesma chave
sao o mesmo recebimento:
  1) PRINCIPAL — (canal, numero do documento). A NF e o boleto que a cobra
     trazem o mesmo numero (no boleto vem como "No documento"); robusto a
     vencimentos diferentes (a NF tem a data do recebimento, o boleto a do
     vencimento).
  2) RESERVA — (canal, valor_total, vencimento), quando nao ha numero.

Modelo do grupo: um documento "principal" (relacionado_id IS NULL) e os demais
apontam pra ele via relacionado_id. A lista mostra so os principais; o detalhe
mostra o par (property `ligados`, bidirecional).
"""
import re
from collections import defaultdict

from app.extensions import db
from app.models import ContaPagar


def _norm_doc(numero):
    """So digitos, sem zeros a esquerda — pra casar nf_numero entre NF e boleto.
    '000053498' -> '53498'; 'NF 3926' -> '3926'."""
    if not numero:
        return ''
    dig = re.sub(r'\D', '', str(numero))
    return dig.lstrip('0') or dig


def _chave(c):
    """Chave do recebimento. None = nao agrupavel (faltam dados)."""
    if not c.origem_canal or c.status == 'ignorado':
        return None
    doc = _norm_doc(c.nf_numero)
    if doc:
        return ('doc', c.origem_canal, doc)
    if c.valor_total is not None and c.vencimento is not None:
        return ('vv', c.origem_canal, str(c.valor_total), c.vencimento)
    return None


def tentar_agrupar(conta):
    """Liga `conta` a um documento ja existente do mesmo recebimento.
    Retorna True se agrupou. Usar logo apos criar uma conta nova."""
    if conta.relacionado_id is not None:
        return False
    k = _chave(conta)
    if k is None:
        return False
    principal = next(
        (c for c in ContaPagar.query.filter(
            ContaPagar.id != conta.id,
            ContaPagar.origem_canal == conta.origem_canal,
            ContaPagar.status != 'ignorado',
            ContaPagar.relacionado_id.is_(None)).order_by(ContaPagar.id.asc()).all()
         if _chave(c) == k), None)
    if principal is None:
        return False
    conta.relacionado_id = principal.id
    db.session.commit()
    return True


def agrupar_automatico():
    """Varre todas as contas e junta os recebimentos (retroativo). Idempotente.
    Retorna nº de documentos agrupados nesta passada."""
    contas = (ContaPagar.query
              .filter(ContaPagar.status != 'ignorado')
              .order_by(ContaPagar.id.asc()).all())
    buckets = defaultdict(list)
    for c in contas:
        k = _chave(c)
        if k is not None:
            buckets[k].append(c)

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

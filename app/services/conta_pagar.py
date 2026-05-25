"""Logica de dominio de Contas a Pagar.

Agrupamento de NF + boleto do mesmo recebimento. Criterios DETERMINISTICOS e
conservadores (dinheiro tem peso):
  1) PRINCIPAL — mesmo canal + MESMO numero de documento. A NF e o boleto que
     a cobra trazem o mesmo numero (no boleto vem como "No documento"); robusto
     a vencimentos diferentes (a NF tem a data do recebimento, o boleto a do
     vencimento).
  2) RESERVA — mesmo canal + MESMO valor_total + MESMO vencimento.
Em ambos exige UNICIDADE (exatamente 1 candidato) pra nunca juntar recebimentos
diferentes por coincidencia.

Modelo do grupo: um documento "principal" (relacionado_id IS NULL) e os demais
apontam pra ele via relacionado_id. A lista mostra so os principais; o detalhe
mostra o par (property `ligados`, bidirecional).
"""
import re

from app.extensions import db
from app.models import ContaPagar


def _norm_doc(numero):
    """So digitos, sem zeros a esquerda — pra casar nf_numero entre NF e boleto.
    '000053498' -> '53498'; 'NF 3926' -> '3926'."""
    if not numero:
        return ''
    dig = re.sub(r'\D', '', str(numero))
    return dig.lstrip('0') or dig


def _principal_por_documento(conta):
    """Documento principal do mesmo recebimento, casado pelo numero do
    documento. So casa se houver EXATAMENTE um candidato (evita ambiguidade)."""
    doc = _norm_doc(conta.nf_numero)
    if not doc or not conta.origem_canal:
        return None
    cands = [c for c in ContaPagar.query.filter(
                ContaPagar.id != conta.id,
                ContaPagar.origem_canal == conta.origem_canal,
                ContaPagar.status != 'ignorado',
                ContaPagar.relacionado_id.is_(None)).all()
             if _norm_doc(c.nf_numero) == doc]
    return cands[0] if len(cands) == 1 else None


def _principal_por_valor_venc(conta):
    """Reserva: casa por canal + valor + vencimento, com unicidade."""
    if (conta.valor_total is None or conta.vencimento is None
            or not conta.origem_canal):
        return None
    cands = (ContaPagar.query.filter(
                ContaPagar.id != conta.id,
                ContaPagar.origem_canal == conta.origem_canal,
                ContaPagar.valor_total == conta.valor_total,
                ContaPagar.vencimento == conta.vencimento,
                ContaPagar.status != 'ignorado',
                ContaPagar.relacionado_id.is_(None))
             .order_by(ContaPagar.id.asc()).all())
    return cands[0] if len(cands) == 1 else None


def _achar_principal(conta):
    return _principal_por_documento(conta) or _principal_por_valor_venc(conta)


def tentar_agrupar(conta):
    """Liga `conta` a um documento ja existente do mesmo recebimento.
    Retorna True se agrupou. Usar logo apos criar uma conta nova."""
    if conta.relacionado_id is not None or conta.status == 'ignorado':
        return False
    principal = _achar_principal(conta)
    if principal is None:
        return False
    conta.relacionado_id = principal.id
    db.session.commit()
    return True


def agrupar_automatico():
    """Varre todas as contas e junta os recebimentos (retroativo). Idempotente.
    Retorna nº de documentos agrupados nesta passada."""
    contas = (ContaPagar.query
              .filter(ContaPagar.status != 'ignorado',
                      ContaPagar.relacionado_id.is_(None))
              .order_by(ContaPagar.id.asc()).all())
    n = 0
    for c in contas:
        if c.relacionado_id is not None:   # virou ligado num passo anterior
            continue
        principal = _achar_principal(c)
        if principal is not None and principal.id != c.id:
            c.relacionado_id = principal.id
            n += 1
    db.session.commit()
    return n

"""Fase 6 — recompensas e resgate (§10).

Débito acontece na APROVAÇÃO (não na solicitação). Saldo nunca fica negativo:
a aprovação trava a linha do funcionário (SELECT ... FOR UPDATE) e valida o
saldo dentro da MESMA transação (§9.3, critério 14). Cancelar após débito gera
estorno positivo. Estoque decrementa na aprovação.
"""
from app.extensions import db
from app.models import (
    Funcionario,
    TreinoEventoPontos,
    TreinoRecompensa,
    TreinoResgate,
)
from app.services import treino_ledger as ledger
from app.utils import agora


class ResgateError(ValueError):
    pass


def solicitar(funcionario, recompensa, temporada):
    """Funcionário solicita — NÃO debita ainda (§10)."""
    if not recompensa.ativa:
        raise ResgateError('Recompensa indisponível.')
    r = TreinoResgate(
        funcionario_id=funcionario.id, recompensa_id=recompensa.id,
        temporada_id=temporada.id, status='SOLICITADO')
    db.session.add(r)
    db.session.commit()
    return r


def aprovar(resgate, *, decidido_por_id):
    """Aprova e DEBITA — dentro de uma transação que trava a linha do
    funcionário pra impedir gasto duplo concorrente (critério 14). Recusa se
    saldo insuficiente ou estoque zerado. Saldo nunca fica negativo."""
    if resgate.status != 'SOLICITADO':
        raise ResgateError('Resgate não está pendente.')
    recompensa = db.session.get(TreinoRecompensa, resgate.recompensa_id)
    # Trava a linha do funcionário: serializa débitos concorrentes (no-op em
    # SQLite; efetivo no Postgres de prod).
    db.session.query(Funcionario).filter_by(
        id=resgate.funcionario_id).with_for_update().first()
    custo = int(recompensa.custo_pontos)
    saldo = ledger.saldo(resgate.funcionario_id, resgate.temporada_id)
    if saldo < custo:
        raise ResgateError(
            f'Saldo insuficiente ({saldo} < {custo}). Resgate recusado.')
    if recompensa.estoque is not None and recompensa.estoque <= 0:
        raise ResgateError('Recompensa esgotada.')

    func = db.session.get(Funcionario, resgate.funcionario_id)
    ev, _ = ledger.creditar(
        func, 'RESGATE', -custo, referencia_tipo='resgate',
        referencia_id=resgate.id, criado_por_id=decidido_por_id,
        observacao=f'resgate {recompensa.nome}', aplica_teto=False)
    resgate.status = 'APROVADO'
    resgate.pontos_debitados = custo
    resgate.evento_debito_id = ev.id if ev else None
    resgate.decidido_em = agora()
    resgate.decidido_por_id = decidido_por_id
    if recompensa.estoque is not None:
        recompensa.estoque = max(0, recompensa.estoque - 1)
    db.session.commit()
    return resgate


def recusar(resgate, *, decidido_por_id):
    if resgate.status != 'SOLICITADO':
        raise ResgateError('Resgate não está pendente.')
    resgate.status = 'CANCELADO'
    resgate.decidido_em = agora()
    resgate.decidido_por_id = decidido_por_id
    db.session.commit()
    return resgate


def entregar(resgate):
    if resgate.status != 'APROVADO':
        raise ResgateError('Só entrega resgate aprovado.')
    resgate.status = 'ENTREGUE'
    db.session.commit()
    return resgate


def cancelar(resgate, *, decidido_por_id):
    """Cancela após aprovação: estorna o débito (lançamento positivo) e devolve
    o estoque. Idempotente."""
    if resgate.status == 'CANCELADO':
        return resgate
    if resgate.evento_debito_id:
        ev = db.session.get(TreinoEventoPontos, resgate.evento_debito_id)
        if ev is not None:
            ledger.estornar(ev, criado_por_id=decidido_por_id)
        recompensa = db.session.get(TreinoRecompensa, resgate.recompensa_id)
        if recompensa is not None and recompensa.estoque is not None:
            recompensa.estoque += 1
    resgate.status = 'CANCELADO'
    resgate.decidido_em = agora()
    resgate.decidido_por_id = decidido_por_id
    db.session.commit()
    return resgate

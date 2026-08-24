"""Fase 4 — aplicação prática (§8): a MAIOR pontuação do sistema (50 pts).
Registrada por gestor observando a operação. Regras server-side: gestor não
registra pra si; evidência mínima; 1 por (funcionário, trilha, temporada);
estorno só admin (§8).
"""
from app.extensions import db
from app.models import TreinoAplicacaoPratica, TreinoEventoPontos
from app.services import treino_ledger as ledger
from app.services import treino_lideranca as lideranca
from app.services import treino_pontos as cfg
from app.utils import hoje

MIN_EVIDENCIA = 20


class AplicacaoError(ValueError):
    pass


def registrar(gestor, funcionario, trilha, temporada, itens_ok, evidencia, *,
              criado_por_id, is_admin=False):
    """Registra a aplicação prática e credita 50 pts pelo ledger. Levanta
    AplicacaoError nas violações (§8)."""
    if gestor.id == funcionario.id:
        raise AplicacaoError('Gestor não pode registrar aplicação pra si mesmo.')
    if len((evidencia or '').strip()) < MIN_EVIDENCIA:
        raise AplicacaoError(
            f'Evidência precisa de ao menos {MIN_EVIDENCIA} caracteres.')
    if not lideranca.pode_observar(
            gestor, funcionario, is_admin=is_admin):
        raise AplicacaoError(
            'Você só pode observar pessoas da sua equipe direta.')

    try:
        itens_ids = [int(item_id) for item_id in (itens_ok or [])]
    except (TypeError, ValueError):
        raise AplicacaoError('O checklist enviado é inválido.') from None
    checklist = lideranca.checklist_da_trilha(trilha.id)
    itens_validos = {
        item.id for item in lideranca.itens_ativos(checklist)}
    if not itens_validos:
        raise AplicacaoError(
            'Este módulo ainda não tem um checklist de observação.')
    if not set(itens_ids).issubset(itens_validos):
        raise AplicacaoError('O checklist foi alterado. Recarregue a página.')
    if set(itens_ids) != itens_validos:
        raise AplicacaoError(
            'Confirme todos os itens antes de validar a aplicação prática.')
    # Máx. 1 por (funcionário, trilha, temporada) — critério 13.
    ja = TreinoAplicacaoPratica.query.filter_by(
        funcionario_id=funcionario.id, trilha_id=trilha.id,
        temporada_id=temporada.id, status='REGISTRADA').first()
    if ja is not None:
        raise AplicacaoError('Já há aplicação registrada nesta trilha/temporada.')

    ap = TreinoAplicacaoPratica(
        funcionario_id=funcionario.id, trilha_id=trilha.id, gestor_id=gestor.id,
        temporada_id=temporada.id, data=hoje(),
        itens_ok=itens_ids, evidencia=evidencia.strip(),
        status='REGISTRADA')
    db.session.add(ap)
    db.session.commit()
    ledger.creditar(
        funcionario, 'APLICACAO_PRATICA', cfg.valor('APLICACAO_PRATICA'),
        temporada=temporada, referencia_tipo='aplicacao', referencia_id=ap.id,
        criado_por_id=criado_por_id)
    return ap


def estornar(aplicacao, *, criado_por_id):
    """Estorna a aplicação (só admin): marca ESTORNADA e estorna o evento de
    pontos (lançamento negativo, sem apagar). Idempotente."""
    if aplicacao.status == 'ESTORNADA':
        return aplicacao
    ev = TreinoEventoPontos.query.filter_by(
        tipo='APLICACAO_PRATICA', referencia_tipo='aplicacao',
        referencia_id=aplicacao.id, estorno_de_id=None).first()
    if ev is not None:
        ledger.estornar(ev, criado_por_id=criado_por_id)
    aplicacao.status = 'ESTORNADA'
    db.session.commit()
    return aplicacao

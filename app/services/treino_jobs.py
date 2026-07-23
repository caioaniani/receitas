"""Fase 8 — jobs agendados (§13). Timezone BRT, semana ISO.

Todos IDEMPOTENTES (rodar 2x não dobra ponto): garantido pelo UNIQUE do
fechamento + a idempotência do ledger (critério 16).
"""
from datetime import datetime, timedelta

from app.extensions import db
from app.models import (
    Funcionario,
    TreinoFechamentoSemanal,
    TreinoProgressoVideo,
    TreinoQuiz,
    TreinoTentativaQuiz,
)
from app.services import treino_ledger as ledger
from app.services import treino_pontos as cfg
from app.services import treino_quiz as tq


def _intervalo_semana(ano, semana):
    inicio = datetime.strptime(f'{int(ano)}-W{int(semana):02d}-1', '%G-W%V-%u')
    return inicio, inicio + timedelta(days=7)


def _meta_cumprida(funcionario_id, inicio, fim):
    """Meta semanal (§4): ≥1 vídeo concluído E ≥1 quiz aprovado na semana."""
    tem_video = TreinoProgressoVideo.query.filter(
        TreinoProgressoVideo.funcionario_id == funcionario_id,
        TreinoProgressoVideo.concluido_em.isnot(None),
        TreinoProgressoVideo.concluido_em >= inicio,
        TreinoProgressoVideo.concluido_em < fim).first() is not None
    tem_quiz = TreinoTentativaQuiz.query.filter(
        TreinoTentativaQuiz.funcionario_id == funcionario_id,
        TreinoTentativaQuiz.aprovada.is_(True),
        TreinoTentativaQuiz.finalizado_em.isnot(None),
        TreinoTentativaQuiz.finalizado_em >= inicio,
        TreinoTentativaQuiz.finalizado_em < fim).first() is not None
    return tem_video and tem_quiz


def fechamento_semanal(ano, semana_iso):
    """Avalia a meta por funcionário, grava o fechamento, credita STREAK_SEMANAL
    e STREAK_MARCO (4 semanas consecutivas). Idempotente: funcionário já
    processado nessa semana é pulado (critério 16). Retorna nº processado."""
    inicio, fim = _intervalo_semana(ano, semana_iso)
    temp = ledger.temporada_ativa()
    prev_dt = inicio - timedelta(days=1)
    p_ano, p_sem, _ = prev_dt.isocalendar()
    ref_semana = int(ano) * 100 + int(semana_iso)
    processados = 0
    for f in Funcionario.query.filter_by(ativo=True).all():
        if TreinoFechamentoSemanal.query.filter_by(
                funcionario_id=f.id, ano=ano, semana_iso=semana_iso).first():
            continue                                  # idempotência
        meta = _meta_cumprida(f.id, inicio, fim)
        prev = TreinoFechamentoSemanal.query.filter_by(
            funcionario_id=f.id, ano=p_ano, semana_iso=p_sem).first()
        base = prev.semanas_consecutivas if (prev and prev.meta_cumprida) else 0
        consec = base + 1 if meta else 0
        db.session.add(TreinoFechamentoSemanal(
            funcionario_id=f.id, ano=ano, semana_iso=semana_iso,
            meta_cumprida=meta, semanas_consecutivas=consec))
        db.session.commit()
        if meta and temp is not None:
            ledger.creditar(f, 'STREAK_SEMANAL', cfg.valor('STREAK_SEMANAL'),
                            temporada=temp, referencia_tipo='semana',
                            referencia_id=ref_semana)
            if consec and consec % 4 == 0:
                ledger.creditar(f, 'STREAK_MARCO', cfg.valor('STREAK_MARCO'),
                                temporada=temp, referencia_tipo='marco',
                                referencia_id=ref_semana)
        processados += 1
    return processados


def limpeza_tentativas():
    """Finaliza tentativas de quiz abandonadas (passaram do tempo máximo =
    questões × 90s). Idempotente. Retorna nº finalizadas."""
    n = 0
    abertas = TreinoTentativaQuiz.query.filter(
        TreinoTentativaQuiz.finalizado_em.is_(None)).all()
    for t in abertas:
        quiz = db.session.get(TreinoQuiz, t.quiz_id)
        if quiz is None:
            continue
        limite = t.iniciado_em + timedelta(
            seconds=tq.SEG_MAX_POR_QUESTAO * (t.total or
                                              quiz.questoes_por_tentativa))
        if ledger.agora_dt() >= limite:
            tq.finalizar(t, quiz)
            n += 1
    return n

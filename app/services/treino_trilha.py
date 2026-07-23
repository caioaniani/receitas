"""Fase 7 (parte 1) — conclusão de TRILHA + selo (§4, §6, §11).

Trilha concluída = todos os vídeos ativos concluídos + todos os quizzes
aprovados + ≥1 aplicação prática validada (§4). Ao concluir: emite SELO
(idempotente) e credita TRILHA_CONCLUIDA (100). O selo carrega o código de
verificação e a carga horária pro certificado (§11).
"""

from app.extensions import db
from app.models import (
    TreinoAplicacaoPratica,
    TreinoProgressoVideo,
    TreinoQuiz,
    TreinoSelo,
    TreinoTentativaQuiz,
)
from app.services import treino_ledger as ledger
from app.services import treino_pontos as cfg


def _videos_ativos(trilha):
    return [v for v in trilha.videos if v.ativo]


def _quizzes_da_trilha(trilha):
    """Quizzes da trilha: os de nível trilha + os de cada vídeo ativo."""
    ids_video = [v.id for v in _videos_ativos(trilha)]
    q = TreinoQuiz.query.filter(TreinoQuiz.ativo.is_(True)).filter(
        db.or_(TreinoQuiz.trilha_id == trilha.id,
               TreinoQuiz.video_id.in_(ids_video) if ids_video else False))
    return q.all()


def progresso_trilha(funcionario, trilha, temporada):
    """Estado da trilha pro funcionário: vídeos ok, quizzes ok, aplicação ok."""
    videos = _videos_ativos(trilha)
    concluidos = {p.video_id for p in TreinoProgressoVideo.query.filter(
        TreinoProgressoVideo.funcionario_id == funcionario.id,
        TreinoProgressoVideo.concluido_em.isnot(None),
        TreinoProgressoVideo.video_id.in_([v.id for v in videos] or [0])).all()}
    videos_ok = all(v.id in concluidos for v in videos) if videos else False

    quizzes = _quizzes_da_trilha(trilha)
    quizzes_ok = True
    for quiz in quizzes:
        aprovada = TreinoTentativaQuiz.query.filter_by(
            funcionario_id=funcionario.id, quiz_id=quiz.id,
            aprovada=True).first()
        if aprovada is None:
            quizzes_ok = False
            break

    aplicacao_ok = TreinoAplicacaoPratica.query.filter_by(
        funcionario_id=funcionario.id, trilha_id=trilha.id,
        temporada_id=temporada.id, status='REGISTRADA').first() is not None

    return {'videos': len(videos), 'videos_ok': videos_ok,
            'quizzes': len(quizzes), 'quizzes_ok': quizzes_ok,
            'aplicacao_ok': aplicacao_ok,
            'completa': bool(videos and videos_ok and quizzes_ok
                             and aplicacao_ok)}


def verificar_conclusao(funcionario, trilha, temporada):
    """Se a trilha está completa, emite o SELO (idempotente) e credita
    TRILHA_CONCLUIDA. Retorna o selo (novo ou já existente) ou None."""
    est = progresso_trilha(funcionario, trilha, temporada)
    if not est['completa']:
        return None
    selo = TreinoSelo.query.filter_by(
        funcionario_id=funcionario.id, trilha_id=trilha.id).first()
    if selo is None:
        carga = sum(round((v.duracao_segundos or 0) / 60 + 0.999)  # ceil min
                    for v in _videos_ativos(trilha)) or \
            (trilha.carga_horaria_minutos or 0)
        selo = TreinoSelo(funcionario_id=funcionario.id, trilha_id=trilha.id,
                          carga_horaria_minutos=carga)
        db.session.add(selo)
        db.session.commit()
    ledger.creditar(
        funcionario, 'TRILHA_CONCLUIDA', cfg.valor('TRILHA_CONCLUIDA'),
        temporada=temporada, referencia_tipo='trilha', referencia_id=trilha.id)
    return selo

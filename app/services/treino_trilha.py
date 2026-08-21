"""Fase 7 (parte 1) — conclusão de TRILHA + selo (§4, §6, §11).

Trilha concluída = todos os vídeos ativos concluídos + todos os quizzes
aprovados + ≥1 aplicação prática validada (§4). Ao concluir: emite SELO
(idempotente) e credita TRILHA_CONCLUIDA (100). O selo carrega o código de
verificação e a carga horária pro certificado (§11).
"""

import math

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


def videos_publicados(trilha):
    """Aulas que de fato podem aparecer para o funcionário.

    ``ativo`` é a decisão editorial do admin; ``video_externo_id`` confirma
    que existe um arquivo associado. A carga inicial antiga deixou os 140
    títulos ativos, mas sem vídeo, então considerar apenas ``ativo`` publicava
    o índice inteiro quando o módulo era ligado.
    """
    return [v for v in trilha.videos if v.ativo and v.video_externo_id]


def _videos_ativos(trilha):
    """Compatibilidade interna: "ativo" no progresso significa publicado."""
    return videos_publicados(trilha)


def quizzes_publicados(trilha):
    """Avaliações publicadas do módulo e de suas aulas publicadas."""
    ids_video = [v.id for v in _videos_ativos(trilha)]
    q = TreinoQuiz.query.filter(TreinoQuiz.ativo.is_(True)).filter(
        db.or_(TreinoQuiz.trilha_id == trilha.id,
               TreinoQuiz.video_id.in_(ids_video) if ids_video else False))
    return q.all()


def _quizzes_da_trilha(trilha):
    """Compatibilidade interna com o nome usado antes da tela v2."""
    return quizzes_publicados(trilha)


def progresso_trilha(funcionario, trilha, temporada):
    """Estado da trilha pro funcionário: vídeos ok, quizzes ok, aplicação ok +
    `percentual` (0-100) pra barra de progresso."""
    videos = _videos_ativos(trilha)
    concluidos = {p.video_id for p in TreinoProgressoVideo.query.filter(
        TreinoProgressoVideo.funcionario_id == funcionario.id,
        TreinoProgressoVideo.concluido_em.isnot(None),
        TreinoProgressoVideo.video_id.in_([v.id for v in videos] or [0])).all()}
    n_videos_ok = sum(1 for v in videos if v.id in concluidos)
    videos_ok = bool(videos) and n_videos_ok == len(videos)

    quizzes = _quizzes_da_trilha(trilha)
    n_quizzes_ok = 0
    for quiz in quizzes:
        if TreinoTentativaQuiz.query.filter_by(
                funcionario_id=funcionario.id, quiz_id=quiz.id,
                aprovada=True).first() is not None:
            n_quizzes_ok += 1
    quizzes_ok = n_quizzes_ok == len(quizzes)

    aplicacao_ok = TreinoAplicacaoPratica.query.filter_by(
        funcionario_id=funcionario.id, trilha_id=trilha.id,
        temporada_id=temporada.id, status='REGISTRADA').first() is not None

    completa = bool(videos and videos_ok and quizzes_ok and aplicacao_ok)

    # Barra de progresso: cada aula + cada quiz + a aplicação prática = 1
    # unidade (o MESMO trio que fecha a trilha — §4). 100% SÓ quando completa
    # (mesmo critério do selo ✓); antes disso, fração das unidades feitas com
    # teto de 99% pra nunca "mentir concluída" (ex.: trilha sem vídeo, ou tudo
    # feito menos a aplicação).
    total_un = len(videos) + len(quizzes) + 1        # +1 = aplicação prática
    ok_un = n_videos_ok + n_quizzes_ok + (1 if aplicacao_ok else 0)
    percentual = 100 if completa else min(99, round(100 * ok_un / total_un))

    return {'videos': len(videos), 'videos_ok': videos_ok,
            'videos_feitos': n_videos_ok,
            'quizzes': len(quizzes), 'quizzes_ok': quizzes_ok,
            'quizzes_feitos': n_quizzes_ok,
            'aplicacao_ok': aplicacao_ok,
            'completa': completa,
            'percentual': percentual}


def verificar_conclusao(funcionario, trilha, temporada):
    """Se a trilha está completa, emite o SELO (idempotente) e credita
    TRILHA_CONCLUIDA. Retorna o selo (novo ou já existente) ou None."""
    est = progresso_trilha(funcionario, trilha, temporada)
    if not est['completa']:
        return None
    selo = TreinoSelo.query.filter_by(
        funcionario_id=funcionario.id, trilha_id=trilha.id).first()
    if selo is None:
        carga = sum(math.ceil((v.duracao_segundos or 0) / 60)
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

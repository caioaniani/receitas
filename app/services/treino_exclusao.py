"""Exclusao segura de trilhas do treinamento.

Conteudo de autoria pode ser removido normalmente. Quando a trilha ja gerou
progresso, respostas, aplicacoes ou certificados, a tela explica o bloqueio e
oferece ao admin uma limpeza explicita. Pontos nunca somem do ledger: recebem
um estorno auditavel antes de o progresso de teste ser apagado.
"""

from app.extensions import db
from app.models import (
    TreinoAlternativa,
    TreinoAplicacaoPratica,
    TreinoChecklistAplicacao,
    TreinoCheckpoint,
    TreinoEventoPontos,
    TreinoProgressoVideo,
    TreinoQuestao,
    TreinoQuiz,
    TreinoRespostaCheckpoint,
    TreinoRespostaQuiz,
    TreinoSelo,
    TreinoTentativaQuiz,
    TreinoTrilhaCargo,
    TreinoVideo,
)
from app.services import treino_ledger as ledger

ROTULOS_HISTORICO = {
    'progresso_video': ('vídeo iniciado', 'vídeos iniciados'),
    'resposta_checkpoint': ('checkpoint respondido', 'checkpoints respondidos'),
    'tentativa_quiz': ('tentativa de quiz', 'tentativas de quiz'),
    'aplicacao': ('aplicação prática', 'aplicações práticas'),
    'selo': ('certificado emitido', 'certificados emitidos'),
}


def _somar(mapa, chave, linhas):
    for trilha_id, quantidade in linhas:
        if trilha_id in mapa and quantidade:
            mapa[trilha_id][chave] = int(quantidade)


def mapa_historico(trilhas):
    """Retorna as dependencias que impedem a exclusao simples por trilha.

    As consultas sao agrupadas para a pagina administrativa nao executar uma
    consulta por trilha e por tipo de historico.
    """
    ids = [t.id for t in trilhas]
    mapa = {trilha_id: {} for trilha_id in ids}
    if not ids:
        return mapa

    _somar(mapa, 'progresso_video', (
        db.session.query(
            TreinoVideo.trilha_id,
            db.func.count(TreinoProgressoVideo.id),
        )
        .join(TreinoProgressoVideo,
              TreinoProgressoVideo.video_id == TreinoVideo.id)
        .filter(TreinoVideo.trilha_id.in_(ids))
        .group_by(TreinoVideo.trilha_id)
        .all()
    ))

    _somar(mapa, 'resposta_checkpoint', (
        db.session.query(
            TreinoVideo.trilha_id,
            db.func.count(TreinoRespostaCheckpoint.id),
        )
        .join(TreinoCheckpoint, TreinoCheckpoint.video_id == TreinoVideo.id)
        .join(TreinoRespostaCheckpoint,
              TreinoRespostaCheckpoint.checkpoint_id == TreinoCheckpoint.id)
        .filter(TreinoVideo.trilha_id.in_(ids))
        .group_by(TreinoVideo.trilha_id)
        .all()
    ))

    trilha_do_quiz = db.func.coalesce(
        TreinoQuiz.trilha_id, TreinoVideo.trilha_id)
    _somar(mapa, 'tentativa_quiz', (
        db.session.query(
            trilha_do_quiz,
            db.func.count(TreinoTentativaQuiz.id),
        )
        .select_from(TreinoQuiz)
        .outerjoin(TreinoVideo, TreinoQuiz.video_id == TreinoVideo.id)
        .join(TreinoTentativaQuiz,
              TreinoTentativaQuiz.quiz_id == TreinoQuiz.id)
        .filter(trilha_do_quiz.in_(ids))
        .group_by(trilha_do_quiz)
        .all()
    ))

    for chave, modelo in (
        ('aplicacao', TreinoAplicacaoPratica),
        ('selo', TreinoSelo),
    ):
        _somar(mapa, chave, (
            db.session.query(modelo.trilha_id, db.func.count(modelo.id))
            .filter(modelo.trilha_id.in_(ids))
            .group_by(modelo.trilha_id)
            .all()
        ))
    return mapa


def resumo_historico(trilha):
    return mapa_historico([trilha]).get(trilha.id, {})


def descricao_historico(resumo):
    partes = []
    for chave, quantidade in resumo.items():
        singular, plural = ROTULOS_HISTORICO[chave]
        partes.append(f'{quantidade} {singular if quantidade == 1 else plural}')
    return ', '.join(partes)


def _dependencias(trilha):
    videos = list(trilha.videos)
    video_ids = [v.id for v in videos]
    checkpoints = TreinoCheckpoint.query.filter(
        TreinoCheckpoint.video_id.in_(video_ids or [0])).all()
    checkpoint_ids = [c.id for c in checkpoints]
    quizzes = TreinoQuiz.query.filter(db.or_(
        TreinoQuiz.trilha_id == trilha.id,
        TreinoQuiz.video_id.in_(video_ids) if video_ids else db.false(),
    )).all()
    quiz_ids = [q.id for q in quizzes]
    questoes = TreinoQuestao.query.filter(
        TreinoQuestao.quiz_id.in_(quiz_ids or [0])).all()
    questao_ids = [q.id for q in questoes]
    alternativas = TreinoAlternativa.query.filter(
        TreinoAlternativa.questao_id.in_(questao_ids or [0])).all()
    tentativas = TreinoTentativaQuiz.query.filter(
        TreinoTentativaQuiz.quiz_id.in_(quiz_ids or [0])).all()
    tentativa_ids = [t.id for t in tentativas]
    aplicacoes = TreinoAplicacaoPratica.query.filter_by(
        trilha_id=trilha.id).all()
    return {
        'videos': videos,
        'video_ids': video_ids,
        'checkpoints': checkpoints,
        'checkpoint_ids': checkpoint_ids,
        'quizzes': quizzes,
        'quiz_ids': quiz_ids,
        'questoes': questoes,
        'alternativas': alternativas,
        'tentativas': tentativas,
        'tentativa_ids': tentativa_ids,
        'aplicacoes': aplicacoes,
        'aplicacao_ids': [a.id for a in aplicacoes],
    }


def _eventos_da_trilha(trilha, deps):
    filtros = [db.and_(
        TreinoEventoPontos.referencia_tipo == 'trilha',
        TreinoEventoPontos.referencia_id == trilha.id,
    )]
    for tipo, ids in (
        ('video', deps['video_ids']),
        ('checkpoint', deps['checkpoint_ids']),
        ('quiz', deps['quiz_ids']),
        ('tentativa', deps['tentativa_ids']),
        ('aplicacao', deps['aplicacao_ids']),
    ):
        if ids:
            filtros.append(db.and_(
                TreinoEventoPontos.referencia_tipo == tipo,
                TreinoEventoPontos.referencia_id.in_(ids),
            ))
    return TreinoEventoPontos.query.filter(
        TreinoEventoPontos.estorno_de_id.is_(None),
        db.or_(*filtros),
    ).all()


def _estornar_pontos(trilha, deps, criado_por_id):
    eventos = _eventos_da_trilha(trilha, deps)
    ids = [evento.id for evento in eventos]
    ja_estornados = set()
    if ids:
        ja_estornados = {
            evento.estorno_de_id for evento in TreinoEventoPontos.query.filter(
                TreinoEventoPontos.estorno_de_id.in_(ids)).all()
        }
    novos = 0
    for evento in eventos:
        if evento.id in ja_estornados:
            continue
        db.session.add(TreinoEventoPontos(
            funcionario_id=evento.funcionario_id,
            unidade_id=evento.unidade_id,
            temporada_id=evento.temporada_id,
            tipo=ledger.TIPO_ESTORNO,
            referencia_tipo=evento.referencia_tipo,
            referencia_id=evento.referencia_id,
            pontos=-int(evento.pontos or 0),
            criado_por_id=criado_por_id,
            observacao=(f'limpeza da trilha de teste "{trilha.nome}"; '
                        f'estorno do evento {evento.id}'),
            estorno_de_id=evento.id,
        ))
        novos += 1
    return novos


def excluir(trilha, *, apagar_historico=False, criado_por_id=None):
    """Exclui a trilha e devolve estatisticas da limpeza.

    Com historico, exige ``apagar_historico=True``. Os pontos sao estornados,
    nunca apagados, preservando a auditoria do ledger.
    """
    resumo = resumo_historico(trilha)
    if resumo and not apagar_historico:
        raise ValueError(descricao_historico(resumo))

    deps = _dependencias(trilha)
    estornos = _estornar_pontos(trilha, deps, criado_por_id) \
        if apagar_historico else 0
    uids_cloudflare = [v.video_externo_id for v in deps['videos']
                       if v.video_externo_id]

    if apagar_historico:
        for resposta in TreinoRespostaQuiz.query.filter(
                TreinoRespostaQuiz.tentativa_id.in_(
                    deps['tentativa_ids'] or [0])).all():
            db.session.delete(resposta)
        for tentativa in deps['tentativas']:
            db.session.delete(tentativa)
        for resposta in TreinoRespostaCheckpoint.query.filter(
                TreinoRespostaCheckpoint.checkpoint_id.in_(
                    deps['checkpoint_ids'] or [0])).all():
            db.session.delete(resposta)
        for progresso in TreinoProgressoVideo.query.filter(
                TreinoProgressoVideo.video_id.in_(
                    deps['video_ids'] or [0])).all():
            db.session.delete(progresso)
        for aplicacao in deps['aplicacoes']:
            db.session.delete(aplicacao)
        for selo in TreinoSelo.query.filter_by(trilha_id=trilha.id).all():
            db.session.delete(selo)

    for alternativa in deps['alternativas']:
        db.session.delete(alternativa)
    for questao in deps['questoes']:
        db.session.delete(questao)
    for quiz in deps['quizzes']:
        db.session.delete(quiz)
    for video in deps['videos']:
        db.session.delete(video)
    for checklist in TreinoChecklistAplicacao.query.filter_by(
            trilha_id=trilha.id).all():
        db.session.delete(checklist)
    for vinculo in TreinoTrilhaCargo.query.filter_by(
            trilha_id=trilha.id).all():
        db.session.delete(vinculo)
    db.session.delete(trilha)
    db.session.commit()

    # Limpeza externa e best-effort: o banco ja e a fonte de verdade e a
    # indisponibilidade do Cloudflare nao pode ressuscitar a trilha.
    if uids_cloudflare:
        from app.services import treinamento_stream as stream
        for uid in uids_cloudflare:
            stream.deletar(uid)

    return {
        'historico': sum(resumo.values()),
        'estornos': estornos,
        'videos': len(deps['videos']),
    }

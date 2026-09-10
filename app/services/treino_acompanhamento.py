"""Consulta do aprendizado de uma pessoa, sem emitir selos ou alterar progresso."""
from collections import defaultdict

from sqlalchemy.orm import selectinload

from app.models import (
    TreinoAplicacaoPratica,
    TreinoProgressoVideo,
    TreinoTentativaQuiz,
    TreinoTrilha,
)
from app.services import treino_onboarding as onboarding
from app.services import treino_trilha as conteudo


def _mais_recente(valores):
    return max((valor for valor in valores if valor is not None), default=None)


def progresso_pessoa(funcionario, temporada):
    """Aulas da versão atual, quizzes publicados e prática no ciclo informado.

    O percentual assistido é a média das aulas publicadas, incluindo avanço
    parcial. Não equivale à conclusão do módulo, que também exige avaliações
    e prática. A ausência de ciclo não esconde as aulas nem valida a prática.
    """
    obrigatorias = {t.id for t in onboarding.trilhas_do_cargo(
        funcionario.cargo_id, so_obrigatorias=True)}
    trilhas = (TreinoTrilha.query.filter_by(ativa=True)
               .options(selectinload(TreinoTrilha.videos))
               .order_by(TreinoTrilha.ordem, TreinoTrilha.nome).all())
    trilhas.sort(key=lambda t: t.id not in obrigatorias)
    videos = {t.id: conteudo.videos_publicados(t) for t in trilhas}
    video_ids = [v.id for lista in videos.values() for v in lista]
    registros = TreinoProgressoVideo.query.filter(
        TreinoProgressoVideo.funcionario_id == funcionario.id,
        TreinoProgressoVideo.video_id.in_(video_ids)).all() if video_ids else []
    por_versao = {(p.video_id, p.versao_video): p for p in registros}

    quizzes = {t.id: conteudo.quizzes_publicados(t) for t in trilhas}
    quiz_ids = {q.id for lista in quizzes.values() for q in lista}
    tentativas = defaultdict(list)
    if quiz_ids:
        for tentativa in TreinoTentativaQuiz.query.filter(
                TreinoTentativaQuiz.funcionario_id == funcionario.id,
                TreinoTentativaQuiz.quiz_id.in_(quiz_ids)).order_by(
                    TreinoTentativaQuiz.numero_tentativa.desc()).all():
            tentativas[tentativa.quiz_id].append(tentativa)
    aplicacoes = {}
    if temporada:
        for registro in TreinoAplicacaoPratica.query.filter_by(
                funcionario_id=funcionario.id, temporada_id=temporada.id,
                status='REGISTRADA').order_by(
                    TreinoAplicacaoPratica.data, TreinoAplicacaoPratica.id).all():
            aplicacoes[registro.trilha_id] = registro

    modulos = []
    for trilha in trilhas:
        aulas = []
        for video in videos[trilha.id]:
            progresso = por_versao.get((video.id, video.versao))
            concluido = bool(progresso and progresso.concluido_em)
            percentual = conteudo.percentual_assistido(progresso)
            aulas.append({
                'video': video, 'percentual': percentual,
                'iniciada': progresso is not None, 'concluido': concluido,
                'concluido_em': progresso.concluido_em if progresso else None,
                'ultima_atividade': _mais_recente([
                    progresso.ultimo_heartbeat_em, progresso.iniciado_em,
                    progresso.concluido_em]) if progresso else None,
            })
        avaliacoes = []
        for quiz in quizzes[trilha.id]:
            historico = tentativas[quiz.id]
            ultima = historico[0] if historico else None
            aprovada = any(t.aprovada for t in historico)
            status = ('Aprovada' if aprovada else 'Não iniciada' if not ultima
                      else 'Em andamento' if ultima.finalizado_em is None
                      else 'Precisa refazer')
            avaliacoes.append({
                'quiz': quiz, 'aprovada': aprovada, 'status': status,
                'tentativas': len(historico), 'ultima_tentativa': ultima,
            })
        aplicacao = aplicacoes.get(trilha.id)
        modulos.append({
            'trilha': trilha, 'obrigatoria': trilha.id in obrigatorias,
            'aulas': aulas, 'quizzes': avaliacoes, 'aplicacao': aplicacao,
            'aulas_concluidas': sum(a['concluido'] for a in aulas),
            'percentual_assistido': round(
                sum(a['percentual'] for a in aulas) / len(aulas)) if aulas else 0,
            'completa': bool(aulas and all(a['concluido'] for a in aulas)
                             and all(q['aprovada'] for q in avaliacoes)
                             and aplicacao),
        })
    todas_aulas = [a for modulo in modulos for a in modulo['aulas']]
    return {
        'modulos': modulos, 'aulas_total': len(todas_aulas),
        'aulas_concluidas': sum(a['concluido'] for a in todas_aulas),
        'aulas_iniciadas': sum(a['iniciada'] for a in todas_aulas),
        'percentual_assistido': round(sum(a['percentual'] for a in todas_aulas)
                                     / len(todas_aulas)) if todas_aulas else 0,
        'ultima_atividade': _mais_recente(
            a['ultima_atividade'] for a in todas_aulas),
    }

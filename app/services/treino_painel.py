"""Leituras prontas para as telas do treinamento.

Este módulo não altera progresso, pontos ou publicação. Ele apenas transforma
os dados existentes em respostas simples para três perguntas: o que o
funcionário faz agora, quem da equipe precisa de atenção e como está uma pessoa
na ficha do RH.
"""
from datetime import timedelta

from app.models import (
    TreinoProgressoVideo,
    TreinoSelo,
    TreinoTentativaQuiz,
)
from app.services import treino_onboarding as onboarding
from app.services import treino_trilha
from app.utils import agora


def _progresso_video(funcionario_id, video):
    return TreinoProgressoVideo.query.filter_by(
        funcionario_id=funcionario_id,
        video_id=video.id,
        versao_video=video.versao,
    ).first()


def proximo_passo(funcionario, trilhas, onboarding_ids):
    """Retorna uma única ação útil, priorizando o conteúdo obrigatório."""
    if funcionario is None:
        return None
    ordenadas = sorted(
        trilhas,
        key=lambda t: (t.id not in onboarding_ids, t.ordem, t.id),
    )
    for trilha in ordenadas:
        videos = treino_trilha.videos_publicados(trilha)
        if not videos:
            continue
        for video in videos:
            progresso = _progresso_video(funcionario.id, video)
            if not progresso or not progresso.concluido_em:
                percentual = int(float(progresso.percentual or 0)) \
                    if progresso else 0
                return {
                    'tipo': 'video',
                    'trilha': trilha,
                    'video': video,
                    'percentual': percentual,
                    'rotulo': 'Continuar aula' if percentual else 'Começar aula',
                    'obrigatorio': trilha.id in onboarding_ids,
                }

        quizzes = treino_trilha.quizzes_publicados(trilha)
        for quiz in quizzes:
            passou = TreinoTentativaQuiz.query.filter_by(
                funcionario_id=funcionario.id,
                quiz_id=quiz.id,
                aprovada=True,
            ).first()
            if not passou:
                return {
                    'tipo': 'quiz',
                    'trilha': trilha,
                    'quiz': quiz,
                    'percentual': 0,
                    'rotulo': 'Fazer avaliação',
                    'obrigatorio': trilha.id in onboarding_ids,
                }

    return None


def progresso_dos_videos(funcionario, videos):
    """Mapa usado pela página do módulo para mostrar visto/retomar/concluído."""
    if funcionario is None or not videos:
        return {}
    ids = [v.id for v in videos]
    linhas = TreinoProgressoVideo.query.filter(
        TreinoProgressoVideo.funcionario_id == funcionario.id,
        TreinoProgressoVideo.video_id.in_(ids),
    ).all()
    atuais = {v.id: v.versao for v in videos}
    return {p.video_id: p for p in linhas
            if atuais.get(p.video_id) == p.versao_video}


def resumo_admin(trilhas, funcionarios):
    """Contadores editoriais; somente problemas reais entram em pendências."""
    videos = [v for t in trilhas for v in t.videos]
    sem_arquivo = [v for v in videos if not v.video_externo_id]
    processando = [v for v in videos
                   if v.video_externo_id and not v.duracao_segundos]
    rascunhos = [v for v in videos
                 if v.video_externo_id and v.duracao_segundos and not v.ativo]
    publicados = [v for v in videos if v.video_externo_id and v.ativo]
    sem_acesso = [f for f in funcionarios if not f.usuario_id]
    pendencias = []
    if processando:
        pendencias.append({
            'tipo': 'processando', 'quantidade': len(processando),
            'texto': 'aula(s) ainda processando o vídeo',
        })
    if rascunhos:
        pendencias.append({
            'tipo': 'rascunho', 'quantidade': len(rascunhos),
            'texto': 'aula(s) prontas para revisar e publicar',
        })
    if sem_acesso:
        pendencias.append({
            'tipo': 'acesso', 'quantidade': len(sem_acesso),
            'texto': 'funcionário(s) ainda sem acesso',
        })
    por_trilha = {}
    for trilha in trilhas:
        lista = list(trilha.videos)
        por_trilha[trilha.id] = {
            'total': len(lista),
            'publicadas': sum(bool(v.ativo and v.video_externo_id) for v in lista),
            'rascunhos': sum(bool(v.video_externo_id and not v.ativo) for v in lista),
            'sem_arquivo': sum(not v.video_externo_id for v in lista),
            'processando': sum(bool(v.video_externo_id and not v.duracao_segundos)
                               for v in lista),
        }
    return {
        'modulos_publicados': sum(bool(t.ativa) for t in trilhas),
        'modulos_total': len(trilhas),
        'aulas_publicadas': len(publicados),
        'aulas_sem_arquivo': len(sem_arquivo),
        'funcionarios_com_acesso': len(funcionarios) - len(sem_acesso),
        'funcionarios_total': len(funcionarios),
        'sem_acesso': len(sem_acesso),
        'pendencias': pendencias,
        'por_trilha': por_trilha,
    }


def _ultima_atividade_por_funcionario(funcionarios):
    ids = [f.id for f in funcionarios]
    if not ids:
        return {}
    resultado = {}
    for p in TreinoProgressoVideo.query.filter(
            TreinoProgressoVideo.funcionario_id.in_(ids)).all():
        momento = p.ultimo_heartbeat_em or p.iniciado_em
        atual = resultado.get(p.funcionario_id)
        if momento and (atual is None or momento > atual):
            resultado[p.funcionario_id] = momento
    return resultado


def painel_equipe(funcionarios, temporada):
    """Linhas priorizadas para o gestor, com progresso e última atividade."""
    funcionarios = list(funcionarios)
    progresso_cargo = onboarding.progressao_lote(funcionarios)
    ultimas = _ultima_atividade_por_funcionario(funcionarios)
    limite_parado = agora() - timedelta(days=7)
    prioridade = {
        'sem_acesso': 0, 'parado': 1, 'nao_iniciou': 2,
        'andamento': 3, 'sem_cargo': 4, 'sem_trilha': 5, 'concluido': 6,
    }
    linhas = []
    for funcionario in funcionarios:
        prog = progresso_cargo[funcionario.id]
        ultima = ultimas.get(funcionario.id)
        percentuais = []
        if temporada:
            percentuais = [treino_trilha.progresso_trilha(
                funcionario, item['trilha'], temporada)['percentual']
                for item in prog['itens']]
        percentual = round(sum(percentuais) / len(percentuais)) \
            if percentuais else (100 if prog['apto'] else 0)
        if not funcionario.usuario_id:
            status = 'sem_acesso'
        elif not funcionario.cargo_id:
            status = 'sem_cargo'
        elif not prog['total']:
            status = 'sem_trilha'
        elif prog['apto']:
            status = 'concluido'
        elif ultima is None:
            status = 'nao_iniciou'
        elif ultima < limite_parado:
            status = 'parado'
        else:
            status = 'andamento'
        linhas.append({
            'funcionario': funcionario,
            'progresso': prog,
            'percentual': percentual,
            'ultima_atividade': ultima,
            'status': status,
        })
    linhas.sort(key=lambda item: (
        prioridade[item['status']], item['funcionario'].nome.lower()))
    contagens = {chave: sum(l['status'] == chave for l in linhas)
                 for chave in prioridade}
    contagens['precisam_atencao'] = sum(
        contagens[chave] for chave in ('sem_acesso', 'parado', 'nao_iniciou'))
    return {'linhas': linhas, 'contagens': contagens, 'total': len(linhas)}


def resumo_funcionario(funcionario, temporada):
    """Resumo compacto para aparecer dentro da ficha do RH."""
    prog = onboarding.progressao(funcionario)
    percentuais = []
    if temporada:
        percentuais = [treino_trilha.progresso_trilha(
            funcionario, item['trilha'], temporada)['percentual']
            for item in prog['itens']]
    percentual = round(sum(percentuais) / len(percentuais)) \
        if percentuais else (100 if prog['apto'] else 0)
    ultima = _ultima_atividade_por_funcionario([funcionario]).get(funcionario.id)
    selos = TreinoSelo.query.filter_by(funcionario_id=funcionario.id).order_by(
        TreinoSelo.emitido_em.desc()).all()
    return {
        'tem_acesso': bool(funcionario.usuario_id),
        'login': funcionario.usuario.login if funcionario.usuario else None,
        'progresso': prog,
        'percentual': percentual,
        'ultima_atividade': ultima,
        'selos': selos,
    }

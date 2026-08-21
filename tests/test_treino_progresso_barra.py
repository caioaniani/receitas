"""Barra de progresso da trilha: `percentual` granular em `progresso_trilha`
(cada aula + cada quiz + a aplicação prática = 1 unidade; 100% SÓ quando a
trilha está concluída — mesmo trio do selo ✓)."""
from datetime import timedelta

from app.extensions import db
from app.models import (
    Funcionario,
    Loja,
    TreinoAplicacaoPratica,
    TreinoProgressoVideo,
    TreinoQuiz,
    TreinoTemporada,
    TreinoTentativaQuiz,
    TreinoTrilha,
    TreinoVideo,
)
from app.services import treino_trilha as tt
from app.utils import agora, hoje


def _base():
    temp = TreinoTemporada(nome='T', inicio=hoje() - timedelta(days=1),
                           fim=hoje() + timedelta(days=30), status='ATIVA')
    loja = Loja(nome='Brooklin', ativa=True)
    db.session.add_all([temp, loja])
    db.session.commit()
    f = Funcionario(nome='Ana', cpf='2', ativo=True)
    f.lojas.append(loja)
    trilha = TreinoTrilha(nome='Seg')
    db.session.add_all([f, trilha])
    db.session.commit()
    return temp, f, trilha


def test_progresso_zero_quando_nada_feito(app):
    with app.app_context():
        temp, f, trilha = _base()
        db.session.add_all([
            TreinoVideo(trilha_id=trilha.id, titulo='A1', ordem=1,
                        video_externo_id='1' * 32),
            TreinoVideo(trilha_id=trilha.id, titulo='A2', ordem=2,
                        video_externo_id='2' * 32)])
        db.session.commit()
        e = tt.progresso_trilha(f, trilha, temp)
        assert e['percentual'] == 0 and not e['completa']
        assert e['videos'] == 2 and e['videos_feitos'] == 0


def test_progresso_parcial_conta_aulas_vistas(app):
    with app.app_context():
        temp, f, trilha = _base()
        v1 = TreinoVideo(trilha_id=trilha.id, titulo='A1', ordem=1,
                         video_externo_id='1' * 32)
        v2 = TreinoVideo(trilha_id=trilha.id, titulo='A2', ordem=2,
                         video_externo_id='2' * 32)
        db.session.add_all([v1, v2])
        db.session.commit()
        db.session.add(TreinoProgressoVideo(
            funcionario_id=f.id, video_id=v1.id, versao_video=1,
            concluido_em=agora()))
        db.session.commit()
        e = tt.progresso_trilha(f, trilha, temp)
        # 1 de 3 unidades (1 aula vista de 2 + aplicação pendente) = 33%
        assert e['percentual'] == 33
        assert e['videos_feitos'] == 1


def test_progresso_sem_video_nao_chega_a_100(app):
    """Trilha sem aula (só quiz + aplicação) NÃO fecha (§4 exige vídeo) — o
    teto de 99% impede a barra de mentir 'concluída'."""
    with app.app_context():
        temp, f, trilha = _base()
        g = Funcionario(nome='G', cpf='9', ativo=True)
        quiz = TreinoQuiz(trilha_id=trilha.id, titulo='Q', ativo=True)
        db.session.add_all([g, quiz])
        db.session.commit()
        db.session.add(TreinoTentativaQuiz(
            funcionario_id=f.id, quiz_id=quiz.id, numero_tentativa=1,
            questoes_sorteadas=[], aprovada=True, finalizado_em=agora()))
        db.session.add(TreinoAplicacaoPratica(
            funcionario_id=f.id, trilha_id=trilha.id, gestor_id=g.id,
            temporada_id=temp.id, data=hoje(), evidencia='fez certo ' * 4,
            status='REGISTRADA'))
        db.session.commit()
        e = tt.progresso_trilha(f, trilha, temp)
        assert not e['completa'] and e['percentual'] == 99


def test_progresso_100_quando_completa(app):
    with app.app_context():
        temp, f, trilha = _base()
        g = Funcionario(nome='G', cpf='9', ativo=True)
        v = TreinoVideo(trilha_id=trilha.id, titulo='A1',
                        duracao_segundos=60, ordem=1,
                        video_externo_id='1' * 32)
        db.session.add_all([g, v])
        db.session.commit()
        db.session.add(TreinoProgressoVideo(
            funcionario_id=f.id, video_id=v.id, versao_video=1,
            concluido_em=agora()))
        db.session.add(TreinoAplicacaoPratica(
            funcionario_id=f.id, trilha_id=trilha.id, gestor_id=g.id,
            temporada_id=temp.id, data=hoje(), evidencia='fez certo ' * 4,
            status='REGISTRADA'))
        db.session.commit()
        e = tt.progresso_trilha(f, trilha, temp)
        # 1 aula + 0 quiz + aplicação = tudo → completa, 100%
        assert e['completa'] and e['percentual'] == 100

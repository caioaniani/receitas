"""Fase 3 — motor de quiz (§4.1, §9.2). Critérios de aceite 5, 6, 7, 8, 9,
10, 11.
"""
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import (
    Funcionario,
    Loja,
    TreinoAlternativa,
    TreinoEventoPontos,
    TreinoQuestao,
    TreinoQuiz,
    TreinoTemporada,
    TreinoTrilha,
)
from app.services import treino_ledger as ledger
from app.services import treino_quiz as tq
from app.utils import hoje


def _base(n_questoes=10, por_tentativa=10, cooldown=0):
    temp = TreinoTemporada(nome='T', inicio=hoje() - timedelta(days=1),
                           fim=hoje() + timedelta(days=30), status='ATIVA')
    loja = Loja(nome='Brooklin', ativa=True)
    trilha = TreinoTrilha(nome='Trilha')
    db.session.add_all([temp, loja, trilha])
    db.session.commit()
    f = Funcionario(nome='Ana', cpf='111.111.111-11')
    f.lojas.append(loja)
    quiz = TreinoQuiz(trilha_id=trilha.id, titulo='Q',
                      questoes_por_tentativa=por_tentativa,
                      cooldown_minutos=cooldown)
    db.session.add_all([f, quiz])
    db.session.commit()
    for i in range(n_questoes):
        q = TreinoQuestao(quiz_id=quiz.id, enunciado=f'Q{i}?')
        db.session.add(q)
        db.session.commit()
        db.session.add(TreinoAlternativa(questao_id=q.id, texto='certa',
                                         correta=True))
        db.session.add(TreinoAlternativa(questao_id=q.id, texto='errada',
                                         correta=False))
        db.session.commit()
    return temp, f, quiz


def _alt_correta(questao_id):
    return TreinoAlternativa.query.filter_by(
        questao_id=questao_id, correta=True).first()


def _alt_errada(questao_id):
    return TreinoAlternativa.query.filter_by(
        questao_id=questao_id, correta=False).first()


def _responder_tentativa(t, acertos, seg=10):
    """Responde a tentativa: `acertos` primeiras certas, resto erradas."""
    for i, item in enumerate(t.questoes_sorteadas):
        qid = item['questao_id']
        alt = _alt_correta(qid) if i < acertos else _alt_errada(qid)
        tq.responder(t, qid, alt.id, seg)


def test_pode_publicar_exige_banco_3x(app):   # critério 5
    with app.app_context():
        _, _, quiz = _base(n_questoes=14, por_tentativa=5)
        assert tq.pode_publicar(quiz) is False       # 14 < 15
        db.session.add(TreinoQuestao(quiz_id=quiz.id, enunciado='Q15?',
                                     ativa=True))
        db.session.commit()
        db.session.expire(quiz)
        assert tq.pode_publicar(quiz) is True         # 15 >= 3×5


def test_pontuacao_1a_tentativa_8_de_10(app):   # critério 8
    with app.app_context():
        temp, f, quiz = _base(n_questoes=10, por_tentativa=10)
        t = tq.iniciar_tentativa(f, quiz)
        _responder_tentativa(t, acertos=8)
        res = tq.finalizar(t, quiz)
        assert res['acertos'] == 8 and res['aprovada'] is True
        # 16 (20×0.8) + 10 (aprovação) = 26
        assert ledger.saldo(f.id, temp.id) == 26


def test_3a_tentativa_credita_5_sem_bonus_repetido(app):   # critério 9
    with app.app_context():
        temp, f, quiz = _base(n_questoes=2, por_tentativa=2, cooldown=0)
        for _ in range(3):
            t = tq.iniciar_tentativa(f, quiz)
            _responder_tentativa(t, acertos=2)          # 100%
            tq.finalizar(t, quiz)
        # t1: 20 + bônus 10 ; t2: 10 ; t3: 5  -> 45. Bônus só 1x.
        assert ledger.saldo(f.id, temp.id) == 45
        assert TreinoEventoPontos.query.filter_by(
            funcionario_id=f.id, tipo='QUIZ_APROVACAO').count() == 1


def test_tempo_minimo_nao_pontua(app):   # critério 10
    with app.app_context():
        temp, f, quiz = _base(n_questoes=1, por_tentativa=1)
        t = tq.iniciar_tentativa(f, quiz)
        qid = t.questoes_sorteadas[0]['questao_id']
        tq.responder(t, qid, _alt_correta(qid).id, segundos_na_questao=2)  # <4s
        res = tq.finalizar(t, quiz)
        assert res['acertos'] == 0            # correta mas não pontuou
        assert ledger.saldo(f.id, temp.id) == 0


def test_cooldown_bloqueia_segunda_tentativa(app):   # critério 11
    with app.app_context():
        temp, f, quiz = _base(n_questoes=2, por_tentativa=2, cooldown=120)
        t = tq.iniciar_tentativa(f, quiz)
        _responder_tentativa(t, acertos=1)
        tq.finalizar(t, quiz)
        with pytest.raises(tq.CooldownError) as e:
            tq.iniciar_tentativa(f, quiz)
        assert e.value.liberado_em is not None


def test_sorteio_varia_entre_tentativas(app):   # critério 6
    with app.app_context():
        temp, f, quiz = _base(n_questoes=15, por_tentativa=5, cooldown=0)
        conjuntos = set()
        for _ in range(8):
            t = tq.iniciar_tentativa(f, quiz)
            conjuntos.add(tuple(sorted(
                it['questao_id'] for it in t.questoes_sorteadas)))
            t.finalizado_em = None
            db.session.commit()
        assert len(conjuntos) > 1      # não são todos idênticos


def test_ordem_fixa_preservada(app):   # critério 7
    with app.app_context():
        temp, f, quiz = _base(n_questoes=0, por_tentativa=1)
        q = TreinoQuestao(quiz_id=quiz.id, enunciado='Qual?')
        db.session.add(q)
        db.session.commit()
        db.session.add_all([
            TreinoAlternativa(questao_id=q.id, texto='A', correta=False),
            TreinoAlternativa(questao_id=q.id, texto='B', correta=False),
            TreinoAlternativa(questao_id=q.id, texto='Todas as anteriores',
                              correta=True, ordem_fixa=True),
        ])
        db.session.commit()
        fixa = TreinoAlternativa.query.filter_by(
            questao_id=q.id, ordem_fixa=True).first()
        for _ in range(6):
            t = tq.iniciar_tentativa(f, quiz)
            alts = t.questoes_sorteadas[0]['alternativas']
            assert alts[-1]['id'] == fixa.id     # 'todas' sempre por último
            t.finalizado_em = None
            db.session.commit()

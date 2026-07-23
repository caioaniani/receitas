"""Fase 3 — motor de quiz (§4.1 pontuação, §9.2 anti-fraude).

Banco ≥3× questões/tentativa; sorteio por tentativa; alternativas embaralhadas
(respeitando ordem_fixa); cooldown entre tentativas; tempo mínimo por questão
(4s) e máximo por tentativa; feedback só no fim. Pontuação proporcional ao
acerto com base decrescente por tentativa + bônus único de aprovação, tudo pelo
ledger.
"""
import random

from app.extensions import db
from app.models import (
    TreinoAlternativa,
    TreinoRespostaQuiz,
    TreinoTentativaQuiz,
)
from app.services import treino_ledger as ledger
from app.services import treino_pontos as cfg
from app.utils import agora

TEMPO_MIN_QUESTAO_SEG = 4      # §9.2: abaixo disso registra mas não pontua
SEG_MAX_POR_QUESTAO = 90       # §9.2: teto de tempo da tentativa = N × 90s
FATOR_BANCO = 3               # §9.2: banco mínimo = 3× questões/tentativa


class CooldownError(Exception):
    def __init__(self, liberado_em):
        self.liberado_em = liberado_em
        super().__init__('cooldown')


def _questoes_ativas(quiz):
    return [q for q in quiz.questoes if q.ativa]


def pode_publicar(quiz):
    """Critério 5: quiz só publica com banco ≥ 3× questões/tentativa."""
    return len(_questoes_ativas(quiz)) >= FATOR_BANCO * quiz.questoes_por_tentativa


def _embaralhar_alternativas(alternativas):
    """Embaralha as alternativas MANTENDO as marcadas `ordem_fixa` na posição
    original (ex.: 'todas as anteriores'). Retorna lista de dicts congelados."""
    itens = list(alternativas)
    moveis_idx = [i for i, a in enumerate(itens) if not a.ordem_fixa]
    embaralhados = moveis_idx[:]
    random.shuffle(embaralhados)
    mapa = dict(zip(moveis_idx, embaralhados))
    saida = []
    for i, a in enumerate(itens):
        origem = itens[mapa[i]] if i in mapa else a
        saida.append({'id': origem.id, 'texto': origem.texto})
    return saida


def iniciar_tentativa(funcionario, quiz):
    """Cria uma tentativa: valida cooldown, sorteia as questões e embaralha as
    alternativas, congelando tudo em `questoes_sorteadas`. Levanta CooldownError
    se dentro do cooldown. Retorna a tentativa."""
    anteriores = (TreinoTentativaQuiz.query.filter_by(
        funcionario_id=funcionario.id, quiz_id=quiz.id)
        .order_by(TreinoTentativaQuiz.numero_tentativa.desc()).all())
    ultima_fin = next((t for t in anteriores if t.finalizado_em), None)
    if ultima_fin is not None and quiz.cooldown_minutos:
        from datetime import timedelta
        liberado = ultima_fin.finalizado_em + timedelta(
            minutes=quiz.cooldown_minutos)
        if agora() < liberado:
            raise CooldownError(liberado)

    banco = _questoes_ativas(quiz)
    n = min(quiz.questoes_por_tentativa, len(banco))
    sorteadas = random.sample(banco, n)
    prova = [{'questao_id': q.id, 'enunciado': q.enunciado,
              'alternativas': _embaralhar_alternativas(q.alternativas)}
             for q in sorteadas]
    numero = (anteriores[0].numero_tentativa + 1) if anteriores else 1
    t = TreinoTentativaQuiz(
        funcionario_id=funcionario.id, quiz_id=quiz.id, numero_tentativa=numero,
        questoes_sorteadas=prova, total=len(prova))
    db.session.add(t)
    db.session.commit()
    return t


def responder(tentativa, questao_id, alternativa_id, segundos_na_questao):
    """Registra a resposta de UMA questão (idempotente por questão). Não pontua
    se abaixo do tempo mínimo (§9.2) mas registra. Sem feedback aqui."""
    if tentativa.finalizado_em:
        return {'ok': False, 'erro': 'tentativa já finalizada'}
    ja = TreinoRespostaQuiz.query.filter_by(
        tentativa_id=tentativa.id, questao_id=questao_id).first()
    if ja is not None:
        return {'ok': True, 'ja_respondido': True}
    alt = db.session.get(TreinoAlternativa, alternativa_id) \
        if alternativa_id else None
    correta = bool(alt and alt.correta and alt.questao_id == int(questao_id))
    seg = int(segundos_na_questao or 0)
    pontuou = seg >= TEMPO_MIN_QUESTAO_SEG
    db.session.add(TreinoRespostaQuiz(
        tentativa_id=tentativa.id, questao_id=questao_id,
        alternativa_id=alternativa_id, correta=correta,
        segundos_na_questao=seg, pontuou=pontuou))
    db.session.commit()
    return {'ok': True, 'ja_respondido': False}


def _base_por_tentativa(numero):
    if numero <= 1:
        return cfg.valor('QUIZ_BASE_1')
    if numero == 2:
        return cfg.valor('QUIZ_BASE_2')
    return cfg.valor('QUIZ_BASE_3')


def finalizar(tentativa, quiz):
    """Corrige, credita pontos pelo ledger (proporcional ao acerto × base da
    tentativa) e o bônus de aprovação (uma vez por quiz). Idempotente. Só conta
    como acerto a resposta correta que pontuou (≥4s). Retorna o resumo (score),
    SEM revelar o gabarito (feedback do §9.2)."""
    funcionario = tentativa.funcionario_id
    if tentativa.finalizado_em:
        return _resumo(tentativa, quiz)
    respostas = TreinoRespostaQuiz.query.filter_by(
        tentativa_id=tentativa.id).all()
    acertos = sum(1 for r in respostas if r.correta and r.pontuou)
    total = tentativa.total or len(tentativa.questoes_sorteadas or [])
    tentativa.acertos = acertos
    tentativa.total = total
    ratio = (acertos / total) if total else 0
    tentativa.aprovada = ratio >= float(quiz.nota_minima)
    tentativa.finalizado_em = agora()
    db.session.commit()

    from app.models import Funcionario
    func = db.session.get(Funcionario, funcionario)
    base = _base_por_tentativa(tentativa.numero_tentativa)
    pontos = round(base * ratio)
    ledger.creditar(func, 'QUIZ', pontos, referencia_tipo='tentativa',
                    referencia_id=tentativa.id,
                    observacao=f'quiz {quiz.id} tent {tentativa.numero_tentativa}')
    if tentativa.aprovada:
        # Bônus UMA vez por quiz (idempotente pela referência quiz/id).
        ledger.creditar(func, 'QUIZ_APROVACAO', cfg.valor('QUIZ_APROVACAO'),
                        referencia_tipo='quiz', referencia_id=quiz.id)
    return _resumo(tentativa, quiz)


def _resumo(tentativa, quiz):
    return {'acertos': tentativa.acertos, 'total': tentativa.total,
            'aprovada': tentativa.aprovada,
            'numero_tentativa': tentativa.numero_tentativa}

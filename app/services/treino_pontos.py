"""Valores de pontuação (§4, critério de aceite 22: nada hard-coded fora da
config). A tabela `treino_config_pontos` é a FONTE editável; os defaults abaixo
só a inicializam. A lógica de pontuação lê SEMPRE por `valor(chave)` — nunca
com número cravado no meio do código.
"""
from app.extensions import db
from app.models import TreinoConfigPontos

# Defaults do spec §4. Todos inteiros (pontos nunca são float, §5).
PADRAO = {
    'VIDEO_CONCLUIDO': 10,
    'CHECKPOINT_CORRETO': 5,
    'QUIZ_APROVACAO': 10,
    'APLICACAO_PRATICA': 50,
    'STREAK_SEMANAL': 15,
    'STREAK_MARCO': 50,
    'TRILHA_CONCLUIDA': 100,
    'TETO_DIARIO_PONTOS': 200,
    # Base da fórmula do quiz por tentativa (§4.1): 1ª/2ª/3ª-em-diante.
    'QUIZ_BASE_1': 20,
    'QUIZ_BASE_2': 10,
    'QUIZ_BASE_3': 5,
    # Faixas de nível por pontos na temporada (§6). Bronze = 0.
    'NIVEL_PRATA': 300,
    'NIVEL_OURO': 800,
    'NIVEL_DIAMANTE': 1500,
}


def valor(chave):
    """Pontos configurados pra `chave`: a linha da tabela (se semeada), senão o
    default do código. Leitura PURA (sem escrever) — não contamina transação.
    KeyError se a chave não existe nem no PADRAO."""
    row = db.session.get(TreinoConfigPontos, chave)
    if row is not None:
        return row.valor
    if chave not in PADRAO:
        raise KeyError(f'config de pontos desconhecida: {chave}')
    return PADRAO[chave]


def garantir_padrao():
    """Semeia na tabela os valores que faltam (idempotente) — chamar 1x (admin
    ou setup) pra tornar tudo editável na tela. Não sobrescreve valores já
    ajustados pelo dono."""
    mudou = False
    for chave, v in PADRAO.items():
        if db.session.get(TreinoConfigPontos, chave) is None:
            db.session.add(TreinoConfigPontos(chave=chave, valor=v))
            mudou = True
    if mudou:
        db.session.commit()


def todos():
    """Mapa chave->valor efetivo (tabela ou default) — pra a tela de admin."""
    return {chave: valor(chave) for chave in PADRAO}

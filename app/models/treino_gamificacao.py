"""Gamificação de treinamento — FUNDAÇÃO (Fase 1 do spec v1.0, 22/07/2026).

Reusa o que já existe (decisão do dono): unidade = `Loja`, pessoa =
`Funcionario` (RH, com login via `Usuario`). O núcleo é o LEDGER de pontos
append-only: NÃO existe coluna de saldo mutável — saldo é SEMPRE
`SUM(pontos)` (§3.5, §5.1). Ponto nunca é recalculado nem sobrescrito;
correção é ESTORNO (lançamento negativo referenciando o original).

Tabelas namespaced com prefixo `treino_` pra não colidir com nomes genéricos
(temporada/quiz/…) no Postgres compartilhado. Todas NOVAS -> criadas por
`db.create_all` (sem ALTER). Mapa spec -> tabela:
  config_pontuacao -> treino_config_pontos
  temporada        -> treino_temporada
  evento_pontos    -> treino_evento_pontos
"""
from app.extensions import db
from app.utils import agora

__all__ = [
    'TreinoConfigPontos',
    'TreinoTemporada',
    'TreinoEventoPontos',
    'TEMPORADA_STATUS',
]

# Status da temporada (String + validação no código — evita criar TYPE ENUM no
# Postgres, alinhado ao resto da casa que usa String pra status).
TEMPORADA_STATUS = ('PLANEJADA', 'ATIVA', 'ENCERRADA')


class TreinoConfigPontos(db.Model):
    """Valores de pontuação EDITÁVEIS (§4: nada hard-coded). chave -> pontos
    (inteiro). Ex.: VIDEO_CONCLUIDO=10, TETO_DIARIO_PONTOS=200. Os defaults
    ficam em `treino_pontos.PADRAO`; esta tabela sobrepõe quando semeada."""
    __tablename__ = 'treino_config_pontos'

    chave = db.Column(db.String(40), primary_key=True)
    valor = db.Column(db.Integer, nullable=False)


class TreinoTemporada(db.Model):
    """Temporada (ciclo de pontuação). Pontos são por temporada; na virada o
    nível reinicia mas o histórico do ledger fica (§6)."""
    __tablename__ = 'treino_temporada'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    inicio = db.Column(db.Date, nullable=False)
    fim = db.Column(db.Date, nullable=False)
    status = db.Column(db.String(12), default='PLANEJADA', nullable=False)


class TreinoEventoPontos(db.Model):
    """LEDGER append-only de pontos (§5.1). Saldo = SUM(pontos). Correção nunca
    apaga nem sobrescreve — é ESTORNO: um lançamento com `pontos` negativos e
    `estorno_de_id` apontando pro original (§3.5, §10).

    O índice ÚNICO PARCIAL (funcionario+tipo+referencia, só entre NÃO-estornos)
    garante IDEMPOTÊNCIA: retry de request ou job duplicado não credita 2x
    (§5.1, critério de aceite 4). Estornos ficam FORA do índice (podem existir
    e não conflitam com o original)."""
    __tablename__ = 'treino_evento_pontos'

    id = db.Column(db.Integer, primary_key=True)
    funcionario_id = db.Column(
        db.Integer, db.ForeignKey('funcionario.id'), nullable=False, index=True)
    # Unidade NO MOMENTO do evento (Loja) — CONGELADA: se a pessoa transfere de
    # loja, o ranking histórico da unidade antiga não se distorce (§5.1). Pode
    # ser NULL (funcionário sem loja vinculada).
    unidade_id = db.Column(db.Integer, db.ForeignKey('loja.id'), index=True)
    temporada_id = db.Column(
        db.Integer, db.ForeignKey('treino_temporada.id'), nullable=False,
        index=True)
    tipo = db.Column(db.String(30), nullable=False)
    referencia_tipo = db.Column(db.String(30))   # 'video','quiz','aplicacao'…
    referencia_id = db.Column(db.Integer)
    pontos = db.Column(db.Integer, nullable=False)
    criado_em = db.Column(db.DateTime, default=agora, nullable=False, index=True)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    observacao = db.Column(db.Text)
    estorno_de_id = db.Column(
        db.Integer, db.ForeignKey('treino_evento_pontos.id'))

    __table_args__ = (
        db.Index(
            'uq_treino_evento_idem',
            'funcionario_id', 'tipo', 'referencia_tipo', 'referencia_id',
            unique=True,
            postgresql_where=db.text('estorno_de_id IS NULL'),
            sqlite_where=db.text('estorno_de_id IS NULL')),
    )

    funcionario = db.relationship('Funcionario')
    temporada = db.relationship('TreinoTemporada')
    estorno_de = db.relationship('TreinoEventoPontos', remote_side=[id])

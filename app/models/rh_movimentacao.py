"""Histórico de cargos independente das tabelas substituídas na importação.

Cada linha é um registro novo: nomes/níveis são fotografias do momento, não
relacionamentos com faixas do plano. ``registrado_em`` nunca significa que a
promoção ocorreu nessa data; ``data_efetiva`` pode permanecer desconhecida.
"""

from app.extensions import db
from app.utils import agora

__all__ = ['RhMovimentacao']


class RhMovimentacao(db.Model):
    __tablename__ = 'rh_movimentacao'
    __table_args__ = (
        db.CheckConstraint(
            "tipo IN ('alteracao', 'promocao', 'aplicacao_plano')",
            name='ck_rh_movimentacao_tipo'),
    )

    id = db.Column(db.Integer, primary_key=True)
    funcionario_id = db.Column(
        db.Integer, db.ForeignKey('funcionario.id'), nullable=False, index=True)
    tipo = db.Column(db.String(30), nullable=False, default='alteracao')
    origem = db.Column(db.String(60), nullable=False)
    registrado_em = db.Column(db.DateTime, nullable=False, default=agora)
    data_efetiva = db.Column(db.Date, nullable=True, index=True)
    registrado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    registrado_por_nome = db.Column(db.String(200))
    # IDs são referência auditável, não FK: renomear/excluir um cargo ou
    # reimportar o plano não reescreve o que estava registrado neste evento.
    cargo_anterior_id = db.Column(db.Integer)
    cargo_anterior = db.Column(db.String(150))
    familia_anterior = db.Column(db.String(100))
    nivel_anterior = db.Column(db.Integer)
    cargo_novo_id = db.Column(db.Integer)
    cargo_novo = db.Column(db.String(150))
    familia_nova = db.Column(db.String(100))
    nivel_novo = db.Column(db.Integer)
    observacao = db.Column(db.Text)

    funcionario = db.relationship('Funcionario')
    registrado_por = db.relationship('Usuario')

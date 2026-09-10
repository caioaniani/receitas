"""Transcrições de origem conhecida, sem inventar datas ou lotes de produção."""

from app.extensions import db
from app.utils import agora


class ProducaoDiarioReferencia(db.Model):
    __tablename__ = 'producao_diario_referencia'

    id = db.Column(db.Integer, primary_key=True)
    chave = db.Column(db.String(200), nullable=False, unique=True)
    produto = db.Column(db.String(200), nullable=False)
    fonte_arquivo = db.Column(db.String(255), nullable=False)
    fonte_sha256 = db.Column(db.String(64), nullable=False)
    fonte_aba = db.Column(db.String(100), nullable=False)
    fonte_linha = db.Column(db.Integer, nullable=False)
    # Preserva valores, classificação, observação e referência original.
    dados = db.Column(db.JSON, nullable=False)
    importado_em = db.Column(db.DateTime, nullable=False, default=agora)
    importado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)

    autor = db.relationship('Usuario', foreign_keys=[importado_por_id])
    __table_args__ = (
        db.CheckConstraint('fonte_linha > 0', name='ck_diario_referencia_linha'),
    )

"""Avisos pra producao — qualquer usuario posta um recado que aparece na TV do
padeiro (barra rolante + campainha) ate alguem confirmar a leitura no touch.

Tabela nova: criada pelo `db.create_all` no startup (sem migration).
"""
from app.extensions import db
from app.utils import agora

__all__ = ['Aviso']


class Aviso(db.Model):
    __tablename__ = 'aviso'

    id = db.Column(db.Integer, primary_key=True)
    texto = db.Column(db.String(500), nullable=False)
    criado_em = db.Column(db.DateTime, default=agora, nullable=False, index=True)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    confirmado_em = db.Column(db.DateTime, nullable=True)
    confirmado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    criado_por = db.relationship('Usuario', foreign_keys=[criado_por_id])
    confirmado_por = db.relationship('Usuario', foreign_keys=[confirmado_por_id])

    @property
    def confirmado(self):
        return self.confirmado_em is not None

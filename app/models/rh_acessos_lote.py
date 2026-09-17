"""Revisões de reenvio já consumidas; não armazena senhas nem e-mails."""
from app.extensions import db
from app.utils import agora

__all__ = ['RhReenvioAcessoExecucao']


class RhReenvioAcessoExecucao(db.Model):
    __tablename__ = 'rh_reenvio_acesso_execucao'
    id = db.Column(db.String(64), primary_key=True)
    autor_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    iniciado_em = db.Column(db.DateTime, default=agora, nullable=False)
    quantidade = db.Column(db.Integer, nullable=False)

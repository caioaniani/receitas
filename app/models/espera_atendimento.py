"""Acompanhamento durável de atendimento transferido; aviso não é resolução."""
from app.extensions import db


class EsperaAtendimento(db.Model):
    __tablename__ = 'espera_atendimento'

    conversa_id = db.Column(db.String(40), primary_key=True)
    inicio_em = db.Column(db.DateTime, nullable=False)
    nome = db.Column(db.String(200))
    mensagem = db.Column(db.Text)
    grave = db.Column(db.Boolean, nullable=False, default=False)
    estado = db.Column(db.String(30), nullable=False, default='aguardando')
    proximo_aviso_em = db.Column(db.DateTime)
    contencao_em = db.Column(db.DateTime)
    resolvido_em = db.Column(db.DateTime)

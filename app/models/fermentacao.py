"""Auditoria e deduplicação da lista diária de fermentação enviada ao Slack."""
from app.extensions import db
from app.utils import agora


class FermentacaoEnvio(db.Model):
    __tablename__ = 'fermentacao_envio'

    # Uma lista por data de preparo, inclusive se o canal for reconfigurado.
    data_alvo = db.Column(db.Date, primary_key=True)
    canal = db.Column(db.String(100), nullable=False)
    estado = db.Column(db.String(20), nullable=False)
    texto = db.Column(db.Text, nullable=False)
    calculo = db.Column(db.JSON, nullable=False)
    slack_ts = db.Column(db.String(50))
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)
    enviado_em = db.Column(db.DateTime)

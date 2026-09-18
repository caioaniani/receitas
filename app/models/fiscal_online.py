"""Documento e cadastro fiscal congelados por pedido, separados da entrega."""
from app.extensions import db
from app.utils import agora


class FiscalPedidoOnline(db.Model):
    __tablename__ = 'fiscal_pedido_online'

    pedido_id = db.Column(db.Integer, db.ForeignKey('pedido_online.id'), primary_key=True)
    documento = db.Column(db.String(14), nullable=False)
    dados = db.Column(db.JSON, nullable=False, default=dict)
    origem = db.Column(db.String(40))
    situacao_ie = db.Column(db.String(30))
    consultado_em = db.Column(db.DateTime)
    confirmado_em = db.Column(db.DateTime)
    confirmado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    erro = db.Column(db.Text)
    criado_em = db.Column(db.DateTime, default=agora, nullable=False)

"""Liberação individual para os pedidos de uma loja à indústria.

Tabela nova, criada por ``db.create_all`` no startup. Revogar não apaga o
vínculo nem a identificação de quem concedeu ou alterou a permissão.
"""

from app.extensions import db
from app.utils import agora


class AcessoPedidosLoja(db.Model):
    __tablename__ = 'acesso_pedidos_loja'

    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), primary_key=True)
    funcionario_id = db.Column(db.Integer, db.ForeignKey('funcionario.id'),
                               nullable=False, index=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False)
    ativo = db.Column(db.Boolean, nullable=False, default=True)
    concedido_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    concedido_em = db.Column(db.DateTime, nullable=False, default=agora)
    atualizado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    atualizado_em = db.Column(db.DateTime, nullable=False, default=agora)

    funcionario = db.relationship('Funcionario')
    loja = db.relationship('Loja')


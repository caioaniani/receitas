"""Recebimento fora do checkout, confirmado pelo owner. Tabela via create_all."""
from app.extensions import db
from app.utils import agora


class PagamentoExternoOnline(db.Model):
    __tablename__ = 'pagamento_externo_online'

    # Uma confirmação integral por pedido, sem alterar a tentativa do gateway.
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedido_online.id'), primary_key=True)
    pagamento_id = db.Column(db.Integer, db.ForeignKey('pagamento_online.id'),
                             nullable=False, unique=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    confirmado_em = db.Column(db.DateTime, nullable=False, default=agora)
    referencia = db.Column(db.String(200), nullable=False)
    valor = db.Column(db.Numeric(10, 2), nullable=False)

    usuario = db.relationship('Usuario')

    @property
    def id(self):
        """Identificador do registro para o AuditLog, sem criar coluna extra."""
        return self.pedido_id

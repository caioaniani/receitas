"""Saída física dos itens sob encomenda do site. Tabela nova via create_all."""
from app.extensions import db
from app.utils import agora


class SaidaProducaoSite(db.Model):
    __tablename__ = 'saida_producao_site'

    # Uma saída por item do pedido, mesmo quando o saldo era insuficiente.
    pedido_item_id = db.Column(db.Integer, db.ForeignKey('pedido_online_item.id'), primary_key=True)
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedido_online.id'), nullable=False, index=True)
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)
    origem = db.Column(db.String(80), nullable=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    # Snapshot da composição escolhida e da baixa real/falta por componente.
    componentes_json = db.Column(db.Text, nullable=False)

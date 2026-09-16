"""Ficha congelada de uma ordem produzida em bateladas padronizadas."""

from app.extensions import db
from app.utils import agora


class PlanejamentoItemBatelada(db.Model):
    __tablename__ = 'planejamento_item_batelada'
    __table_args__ = (
        db.CheckConstraint('bateladas >= 0', name='ck_item_batelada_nao_negativa'),
    )

    item_id = db.Column(
        db.Integer,
        db.ForeignKey('planejamento_item.id', ondelete='CASCADE'),
        primary_key=True,
    )
    # Quantidades POR BATELADA, incluindo IDs: uma edição posterior da ficha
    # não muda o que foi pesado nem os débitos de uma ordem já enviada.
    dados = db.Column(db.JSON, nullable=False)
    bateladas = db.Column(db.Integer, nullable=False)
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)

    item = db.relationship(
        'PlanejamentoItem',
        backref=db.backref(
            'batelada_padrao', uselist=False, cascade='all, delete-orphan',
            single_parent=True,
        ),
    )

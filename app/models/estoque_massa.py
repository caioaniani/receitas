"""Complemento em gramas do estoque histórico de bolas de massa.

Tabelas novas: o estoque e os movimentos antigos continuam em bolas inteiras.
O saldo residual e seu histórico conservam a parcela menor que uma bola.
"""

from decimal import Decimal

from app.extensions import db
from app.utils import agora


class SaldoResidualMassa(db.Model):
    __tablename__ = 'saldo_residual_massa'
    __table_args__ = (
        db.CheckConstraint('peso_bola_g > 0', name='ck_residual_massa_peso_positivo'),
        db.CheckConstraint('g >= 0 AND g < peso_bola_g',
                           name='ck_residual_massa_intervalo'),
    )

    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), primary_key=True)
    g = db.Column(db.Numeric(20, 6), nullable=False, default=Decimal(0),
                  server_default='0')
    # Congelado no primeiro movimento: editar a ficha não revaloriza bolas
    # que já estavam no estoque. A unidade histórica permanece a mesma.
    peso_bola_g = db.Column(db.Numeric(20, 6), nullable=False)
    atualizado_em = db.Column(db.DateTime, nullable=False, default=agora,
                              onupdate=agora)


class MovEstoqueMassa(db.Model):
    __tablename__ = 'mov_estoque_massa'
    __table_args__ = (
        db.CheckConstraint('quantidade_g > 0', name='ck_mov_massa_qtd_positiva'),
        db.CheckConstraint('saldo_anterior_g >= 0 AND saldo_posterior_g >= 0',
                           name='ck_mov_massa_saldos_nao_negativos'),
    )

    id = db.Column(db.Integer, primary_key=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=False,
                           index=True)
    estoque_producao_id = db.Column(db.Integer, db.ForeignKey('estoque_producao.id'),
                                    nullable=False, index=True)
    movimento_inteiro_id = db.Column(db.Integer,
                                     db.ForeignKey('mov_estoque_producao.id'),
                                     nullable=True, unique=True)
    perda_producao_id = db.Column(db.Integer,
                                  db.ForeignKey('perda_producao.id', ondelete='SET NULL'),
                                  nullable=True, index=True)
    estorno_de_id = db.Column(db.Integer, db.ForeignKey('mov_estoque_massa.id'),
                              nullable=True, unique=True)
    tipo = db.Column(db.String(50), nullable=False)
    quantidade_g = db.Column(db.Numeric(20, 6), nullable=False)
    saldo_anterior_g = db.Column(db.Numeric(20, 6), nullable=False)
    saldo_posterior_g = db.Column(db.Numeric(20, 6), nullable=False)
    peso_bola_g = db.Column(db.Numeric(20, 6), nullable=False)
    referencia = db.Column(db.String(200))
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    data = db.Column(db.DateTime, nullable=False, default=agora, index=True)

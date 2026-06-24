"""Modelos do dominio: loja.

Faz parte de `app.models` (split em multiplos arquivos por dominio
em 2026-05-21). Importar via `from app.models import X` continua
funcionando porque `app/models/__init__.py` re-exporta tudo.
"""

from app.extensions import db


class Loja(db.Model):
    __tablename__ = 'loja'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False, unique=True)
    endereco = db.Column(db.String(300))
    telefone = db.Column(db.String(30))
    ativa = db.Column(db.Boolean, default=True)
    planta_imagem = db.Column(db.LargeBinary)
    planta_mimetype = db.Column(db.String(100))
    # PIN 4-6 digitos usado pelo funcionario da loja pra confirmar
    # recebimento via QR Code (handshake driver → loja).
    pin = db.Column(db.String(8))

    def __repr__(self):
        return f'<Loja {self.nome}>'


class PrecoLojaReceita(db.Model):
    __tablename__ = 'preco_loja_receita'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=False)
    preco = db.Column(db.Float, nullable=False)

    __table_args__ = (db.UniqueConstraint('loja_id', 'receita_id', name='uq_preco_loja_receita'),)

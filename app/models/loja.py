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
    # Dados FISCAIS (20/07/2026): a loja e DESTINATARIA da NF-e de
    # transferencia industria→loja (filial com CNPJ proprio). SEFAZ exige
    # documento + endereco estruturado — mesma regua do ClienteB2B. O campo
    # livre `endereco` acima segue como fallback humano (RH/escala).
    cnpj = db.Column(db.String(20))
    inscricao_estadual = db.Column(db.String(20))
    endereco_logradouro = db.Column(db.String(200))
    endereco_numero = db.Column(db.String(20))
    endereco_complemento = db.Column(db.String(100))
    endereco_bairro = db.Column(db.String(100))
    endereco_cep = db.Column(db.String(9))
    endereco_cidade = db.Column(db.String(100))
    endereco_uf = db.Column(db.String(2))

    def __repr__(self):
        return f'<Loja {self.nome}>'


class PrecoLojaReceita(db.Model):
    __tablename__ = 'preco_loja_receita'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=False)
    preco = db.Column(db.Float, nullable=False)

    __table_args__ = (db.UniqueConstraint('loja_id', 'receita_id', name='uq_preco_loja_receita'),)

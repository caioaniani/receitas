"""Intenções duráveis: entrega/fechamento não executam integrações na requisição.

Somente tabelas novas, criadas pelo _setup_schema antes de servir tráfego.
Referências de documentos/atores são snapshots: excluir a origem não apaga a trilha.
"""
from app.extensions import db
from app.utils import agora


class DelegacaoFiscalB2B(db.Model):
    __tablename__ = 'delegacao_fiscal_b2b'
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id', ondelete='CASCADE'), primary_key=True)
    concedida_por_id = db.Column(db.Integer, nullable=False)
    concedida_em = db.Column(db.DateTime, nullable=False, default=agora)


class TentativaNFB2B(db.Model):
    __tablename__ = 'tentativa_nf_b2b'
    chave = db.Column(db.String(60), primary_key=True)  # venda:N / fatura:N
    estado = db.Column(db.String(20), nullable=False)  # iniciada / concluida / conferir
    iniciada_em = db.Column(db.DateTime, nullable=False, default=agora)
    usuario_id = db.Column(db.Integer)
    erro = db.Column(db.String(500))
    assinatura = db.Column(db.String(64))


class AutomacaoCobranca(db.Model):
    __tablename__ = 'automacao_cobranca'
    id = db.Column(db.Integer, primary_key=True)
    chave = db.Column(db.String(60), unique=True, nullable=False)
    tipo = db.Column(db.String(10), nullable=False)  # venda / fatura
    documento_id = db.Column(db.Integer, nullable=False)
    referencia = db.Column(db.String(120), nullable=False)
    usuario_id = db.Column(db.Integer)
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)
    atualizado_em = db.Column(db.DateTime, nullable=False, default=agora)
    estado = db.Column(db.String(30), nullable=False, default='pendente', index=True)
    erro = db.Column(db.String(500))
    cobranca_ids = db.Column(db.JSON, nullable=False, default=list)


class AvisoRemessa(db.Model):
    __tablename__ = 'aviso_remessa'
    id = db.Column(db.Integer, primary_key=True)
    remessa_id = db.Column(db.Integer, nullable=False, index=True)
    destinatario = db.Column(db.String(254), nullable=False)
    estado = db.Column(db.String(20), nullable=False, default='pendente')
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)
    enviado_em = db.Column(db.DateTime)
    provedor_id = db.Column(db.String(150))
    erro = db.Column(db.String(500))
    __table_args__ = (db.UniqueConstraint('remessa_id', 'destinatario', name='uq_aviso_remessa_destino'),)


class ConfirmacaoRegistroBoleto(db.Model):
    __tablename__ = 'confirmacao_registro_boleto'
    cobranca_id = db.Column(db.Integer, primary_key=True)
    remessa_id = db.Column(db.Integer, nullable=False)
    nosso_numero = db.Column(db.String(9), nullable=False)
    valor = db.Column(db.Numeric(10, 2), nullable=False)
    vencimento = db.Column(db.Date, nullable=False)
    usuario_id = db.Column(db.Integer, nullable=False)
    confirmado_em = db.Column(db.DateTime, nullable=False, default=agora)

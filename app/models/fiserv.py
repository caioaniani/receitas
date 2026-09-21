"""Recebimento privado de EDI: somente tabelas novas, fora da auditoria genérica."""

from app.extensions import db
from app.utils import agora


class IntegracaoFiserv(db.Model):
    __tablename__ = 'integracao_fiserv'
    __table_args__ = (db.CheckConstraint('id = 1', name='ck_fiserv_config_unica'),)

    id = db.Column(db.Integer, primary_key=True)
    acesso_cifrado = db.Column(db.Text, nullable=False)
    versao = db.Column(db.Integer, nullable=False, default=1)
    ativa = db.Column(db.Boolean, nullable=False, default=False)
    coleta_solicitada = db.Column(db.Boolean, nullable=False, default=True)
    estado = db.Column(db.String(32), nullable=False, default='aguardando')
    atualizado_em = db.Column(db.DateTime, nullable=False, default=agora)
    atualizado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    ultima_tentativa_em = db.Column(db.DateTime)
    ultimo_sucesso_em = db.Column(db.DateTime)
    mensagem = db.Column(db.String(240))


class ArquivoFiservRecebido(db.Model):
    __tablename__ = 'arquivo_fiserv_recebido'
    __table_args__ = (db.UniqueConstraint('sha256', name='uq_fiserv_arquivo_hash'),)

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(255), nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    tamanho = db.Column(db.Integer, nullable=False)
    modificado_remoto = db.Column(db.BigInteger, nullable=False)
    conteudo_cifrado = db.deferred(db.Column(db.Text, nullable=False))
    recebido_em = db.Column(db.DateTime, nullable=False, default=agora, index=True)
    versao_configuracao = db.Column(db.Integer, nullable=False)


class EventoFiserv(db.Model):
    """Trilha pequena e sanitizada: nenhum segredo ou conteúdo financeiro."""
    __tablename__ = 'evento_fiserv'

    id = db.Column(db.Integer, primary_key=True)
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)
    acao = db.Column(db.String(32), nullable=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    arquivos_novos = db.Column(db.Integer, nullable=False, default=0)


class FiservArquivoRemoto(db.Model):
    """Cache limitado por tempo; revisões com mesmos metadados são revalidadas."""
    __tablename__ = 'fiserv_arquivo_remoto'
    __table_args__ = (db.UniqueConstraint('versao_configuracao', 'nome', name='uq_fiserv_remoto_versao_nome'),)

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(255), nullable=False)
    versao_configuracao = db.Column(db.Integer, nullable=False)
    tamanho = db.Column(db.Integer, nullable=False)
    modificado_remoto = db.Column(db.BigInteger, nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    conferido_em = db.Column(db.DateTime, nullable=False, default=agora)

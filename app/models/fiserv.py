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


class PendenciaFiserv(db.Model):
    """Arquivo ainda não recebido; preservado mesmo se deixar a lista remota."""
    __tablename__ = 'pendencia_fiserv'
    __table_args__ = (
        db.UniqueConstraint('versao_configuracao', 'nome', name='uq_fiserv_pendencia_versao_nome'),
        db.CheckConstraint('tentativas >= 1', name='ck_fiserv_pendencia_tentativas'),
    )

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(255), nullable=False)
    versao_configuracao = db.Column(db.Integer, nullable=False)
    tamanho = db.Column(db.Integer, nullable=False)
    modificado_remoto = db.Column(db.BigInteger, nullable=False)
    etapa = db.Column(db.String(32), nullable=False)
    codigo = db.Column(db.String(32), nullable=False)
    tentativas = db.Column(db.Integer, nullable=False, default=1)
    ultima_tentativa_em = db.Column(db.DateTime, nullable=False, default=agora)
    proxima_tentativa_em = db.Column(db.DateTime, nullable=False)


class InterpretacaoFiserv(db.Model):
    """Resultado versionado de um original; nenhum lançamento de caixa."""
    __tablename__ = 'interpretacao_fiserv'
    __table_args__ = (
        db.UniqueConstraint('arquivo_id', 'versao_parser', name='uq_fiserv_interpretacao_arquivo_versao'),
    )

    id = db.Column(db.Integer, primary_key=True)
    arquivo_id = db.Column(db.Integer, db.ForeignKey('arquivo_fiserv_recebido.id'), nullable=False, index=True)
    versao_parser = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False)
    codigo_erro = db.Column(db.String(40))
    tipo = db.Column(db.String(20), index=True)
    layout = db.Column(db.String(40))
    data_processamento = db.Column(db.Date, index=True)
    numero_processamento = db.Column(db.String(100))
    tipo_processamento = db.Column(db.String(100))
    adquirente = db.Column(db.String(200))
    documento = db.Column(db.String(200), index=True)
    hash_semantico = db.Column(db.String(64), index=True)
    duplicado_de_id = db.Column(db.Integer, db.ForeignKey('interpretacao_fiserv.id'))
    total_registros = db.Column(db.Integer, nullable=False, default=0)
    total_observacoes = db.Column(db.Integer, nullable=False, default=0)
    avisos_json = db.Column(db.JSON, nullable=False, default=list)
    processado_em = db.Column(db.DateTime, nullable=False, default=agora)


class ObservacaoFiserv(db.Model):
    """Observação imutável da fonte; a projeção escolhe as revisões vigentes."""
    __tablename__ = 'observacao_fiserv'
    __table_args__ = (
        db.UniqueConstraint('interpretacao_id', 'ordinal', name='uq_fiserv_observacao_origem'),
        db.Index('ix_fiserv_observacao_identidade', 'categoria', 'chave_negocio'),
    )

    id = db.Column(db.Integer, primary_key=True)
    interpretacao_id = db.Column(db.Integer, db.ForeignKey('interpretacao_fiserv.id'), nullable=False, index=True)
    ordinal = db.Column(db.Integer, nullable=False)
    categoria = db.Column(db.String(30), nullable=False, index=True)
    registro = db.Column(db.String(10), nullable=False)
    chave_negocio = db.Column(db.String(64))
    fingerprint = db.Column(db.String(64), nullable=False)
    documento = db.Column(db.String(200), index=True)
    estabelecimento = db.Column(db.String(200), index=True)
    data_evento = db.Column(db.Date, index=True)
    data_vencimento = db.Column(db.Date, index=True)
    data_atualizacao = db.Column(db.DateTime)
    bandeira = db.Column(db.String(200))
    produto = db.Column(db.String(200))
    referencia = db.Column(db.String(200))
    status = db.Column(db.String(200))
    direcao = db.Column(db.Integer)
    papel = db.Column(db.String(30), nullable=False)
    conjunto = db.Column(db.String(64), index=True)
    incluir_totais = db.Column(db.Boolean, nullable=False, default=False)
    bruto = db.Column(db.Numeric(18, 2))
    taxa = db.Column(db.Numeric(18, 2))
    comissao = db.Column(db.Numeric(18, 2))
    liquido = db.Column(db.Numeric(18, 2))
    antecipacao = db.Column(db.Numeric(18, 2))
    previsto = db.Column(db.Numeric(18, 2))
    liquidado = db.Column(db.Numeric(18, 2))
    atualizado = db.Column(db.Numeric(18, 2))
    livre = db.Column(db.Numeric(18, 2))
    alocado = db.Column(db.Numeric(18, 2))
    ajuste = db.Column(db.Numeric(18, 2))
    movimento = db.Column(db.Numeric(18, 2))
    detalhes_json = db.Column(db.JSON, nullable=False, default=dict)

"""Medições reais dos lotes, independentes de fichas e ordens reconstruíveis."""

from app.extensions import db
from app.utils import agora


class ProducaoDiarioLote(db.Model):
    __tablename__ = 'producao_diario_lote'

    id = db.Column(db.Integer, primary_key=True)
    chave_criacao = db.Column(db.String(36), nullable=False, unique=True)
    tipo_origem = db.Column(db.String(10), nullable=False)
    # Snapshots, deliberadamente sem FK: reconstruir a ordem não apaga o diário.
    origem_id = db.Column(db.Integer, nullable=False)
    receita_id = db.Column(db.Integer)
    planejamento_id = db.Column(db.Integer, nullable=False)
    data_ordem = db.Column(db.Date, nullable=False, index=True)
    data_producao = db.Column(db.Date, nullable=False, index=True)
    nome = db.Column(db.String(200), nullable=False)
    identificacao = db.Column(db.String(100))
    status = db.Column(db.String(20), nullable=False, default='aberto')
    medidas = db.Column(db.JSON, nullable=False, default=dict)
    observacao = db.Column(db.Text)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)
    atualizado_em = db.Column(db.DateTime, nullable=False, default=agora)
    versao = db.Column(db.Integer, nullable=False, default=1)

    autor = db.relationship('Usuario', foreign_keys=[criado_por_id])
    etapas = db.relationship('ProducaoDiarioEtapa', back_populates='lote',
                             order_by='ProducaoDiarioEtapa.id', lazy=True)
    alteracoes = db.relationship('ProducaoDiarioAlteracao', back_populates='lote',
                                 order_by='ProducaoDiarioAlteracao.id', lazy=True)
    __table_args__ = (
        db.CheckConstraint("tipo_origem IN ('item', 'base')", name='ck_diario_tipo_origem'),
        db.CheckConstraint("status IN ('aberto', 'concluido')", name='ck_diario_lote_status'),
        db.CheckConstraint('versao > 0', name='ck_diario_lote_versao'),
    )


class ProducaoDiarioEtapa(db.Model):
    __tablename__ = 'producao_diario_etapa'

    id = db.Column(db.Integer, primary_key=True)
    lote_id = db.Column(db.Integer, db.ForeignKey('producao_diario_lote.id'), nullable=False, index=True)
    nome = db.Column(db.String(100), nullable=False)
    inicio_em = db.Column(db.DateTime, nullable=False)
    fim_em = db.Column(db.DateTime)
    observacao = db.Column(db.Text)
    autor_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)
    atualizado_em = db.Column(db.DateTime, nullable=False, default=agora)

    lote = db.relationship('ProducaoDiarioLote', back_populates='etapas')
    autor = db.relationship('Usuario', foreign_keys=[autor_id])
    __table_args__ = (
        db.CheckConstraint('fim_em IS NULL OR fim_em >= inicio_em', name='ck_diario_etapa_horarios'),
    )

    @property
    def duracao_min(self):
        if self.fim_em is None:
            return None
        return (self.fim_em - self.inicio_em).total_seconds() / 60


class ProducaoDiarioAlteracao(db.Model):
    __tablename__ = 'producao_diario_alteracao'

    id = db.Column(db.Integer, primary_key=True)
    lote_id = db.Column(db.Integer, db.ForeignKey('producao_diario_lote.id'), nullable=False, index=True)
    etapa_id = db.Column(db.Integer, db.ForeignKey('producao_diario_etapa.id'))
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    evento = db.Column(db.String(30), nullable=False)
    antes = db.Column(db.JSON)
    depois = db.Column(db.JSON)
    criado_em = db.Column(db.DateTime, nullable=False, default=agora)

    lote = db.relationship('ProducaoDiarioLote', back_populates='alteracoes')
    etapa = db.relationship('ProducaoDiarioEtapa')
    autor = db.relationship('Usuario', foreign_keys=[usuario_id])

    @property
    def acao(self):
        return self.evento

"""Plano de cargos e carreira importado pelo RH.

As tabelas guardam a proposta e o vínculo com treinamento/funcionários sem
alterar o cargo ou a remuneração contratual. São tabelas novas criadas por
``db.create_all``; não exigem ALTER nas tabelas existentes.
"""

from app.extensions import db
from app.utils import agora

__all__ = [
    'PlanoCarreiraImportacao', 'PlanoCarreiraFaixa', 'PlanoCarreiraRegra',
    'PlanoCarreiraConteudo', 'PlanoCarreiraEnquadramento',
    'PlanoCarreiraValidacao',
]


class PlanoCarreiraImportacao(db.Model):
    __tablename__ = 'plano_carreira_importacao'
    id = db.Column(db.Integer, primary_key=True)
    nome_arquivo = db.Column(db.String(255), nullable=False)
    sha256 = db.Column(db.String(64), nullable=False)
    referencia = db.Column(db.String(100))
    importado_em = db.Column(db.DateTime, default=agora, nullable=False)
    importado_por_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    importado_por = db.relationship('Usuario')


class PlanoCarreiraFaixa(db.Model):
    __tablename__ = 'plano_carreira_faixa'
    __table_args__ = (db.UniqueConstraint('familia', 'nivel', name='uq_carreira_faixa'),)
    id = db.Column(db.Integer, primary_key=True)
    importacao_id = db.Column(db.Integer, db.ForeignKey('plano_carreira_importacao.id'), nullable=False, index=True)
    familia = db.Column(db.String(100), nullable=False, index=True)
    nivel = db.Column(db.Integer, nullable=False)
    cargo_proposto = db.Column(db.String(150), nullable=False)
    papel = db.Column(db.Text)
    unidade = db.Column(db.String(30))
    base_referencia = db.Column(db.Float, default=0)
    multiplicador = db.Column(db.Float, default=1)
    salario_nivel = db.Column(db.Float, default=0)
    horas_mes = db.Column(db.Float, default=0)
    equivalente_mensal = db.Column(db.Float, default=0)
    complemento_funcao = db.Column(db.Float, default=0)
    total_alvo = db.Column(db.Float, default=0)
    videos_minimos = db.Column(db.Integer, default=0)
    tempo_minimo_meses = db.Column(db.Integer, default=0)
    checklist_minimo = db.Column(db.Float, default=0)
    certificacao_pratica = db.Column(db.Text)
    observacao = db.Column(db.Text)


class PlanoCarreiraRegra(db.Model):
    __tablename__ = 'plano_carreira_regra'
    id = db.Column(db.Integer, primary_key=True)
    importacao_id = db.Column(db.Integer, db.ForeignKey('plano_carreira_importacao.id'), nullable=False, index=True)
    transicao = db.Column(db.String(30), nullable=False, unique=True)
    tempo_minimo = db.Column(db.String(150))
    conteudo_minimo = db.Column(db.Text)
    checklist_minimo = db.Column(db.String(50))
    certificacao = db.Column(db.Text)
    evidencia = db.Column(db.Text)
    aprovacao = db.Column(db.Text)
    se_nao_atingir = db.Column(db.Text)


class PlanoCarreiraConteudo(db.Model):
    __tablename__ = 'plano_carreira_conteudo'
    __table_args__ = (db.UniqueConstraint('familia', 'codigo', name='uq_carreira_conteudo'),)
    id = db.Column(db.Integer, primary_key=True)
    importacao_id = db.Column(db.Integer, db.ForeignKey('plano_carreira_importacao.id'), nullable=False, index=True)
    familia = db.Column(db.String(100), nullable=False, index=True)
    codigo = db.Column(db.String(30), nullable=False)
    modulo = db.Column(db.String(150))
    titulo = db.Column(db.String(200), nullable=False)
    categoria = db.Column(db.String(100))
    nivel_minimo = db.Column(db.Integer, nullable=False)
    objetivo = db.Column(db.Text)
    treino_video_id = db.Column(db.Integer, db.ForeignKey('treino_video.id'), index=True)
    treino_video = db.relationship('TreinoVideo')


class PlanoCarreiraEnquadramento(db.Model):
    __tablename__ = 'plano_carreira_enquadramento'
    __table_args__ = (db.UniqueConstraint('funcionario_id', name='uq_carreira_funcionario'),)
    id = db.Column(db.Integer, primary_key=True)
    importacao_id = db.Column(db.Integer, db.ForeignKey('plano_carreira_importacao.id'), nullable=False, index=True)
    funcionario_id = db.Column(db.Integer, db.ForeignKey('funcionario.id'), nullable=False, index=True)
    familia = db.Column(db.String(100), nullable=False, index=True)
    nivel = db.Column(db.Integer)
    cargo_atual_planilha = db.Column(db.String(150))
    salario_base_atual = db.Column(db.Float, default=0)
    complementos_atuais = db.Column(db.Float, default=0)
    total_atual = db.Column(db.Float, default=0)
    cargo_proposto = db.Column(db.String(150))
    salario_base_alvo = db.Column(db.Float, default=0)
    complemento_funcao_alvo = db.Column(db.Float, default=0)
    total_alvo = db.Column(db.Float, default=0)
    status_cenario = db.Column(db.String(80))
    nota_transicao = db.Column(db.Text)
    fonte_competencia = db.Column(db.Text)
    decisao = db.Column(db.String(30))
    funcionario = db.relationship('Funcionario', backref=db.backref('enquadramento_carreira', uselist=False))

    @property
    def diferenca_total(self):
        return (self.total_alvo or 0) - (self.total_atual or 0)


class PlanoCarreiraValidacao(db.Model):
    __tablename__ = 'plano_carreira_validacao'
    id = db.Column(db.Integer, primary_key=True)
    importacao_id = db.Column(db.Integer, db.ForeignKey('plano_carreira_importacao.id'), nullable=False, index=True)
    ordem = db.Column(db.Integer, nullable=False, unique=True)
    tema = db.Column(db.String(200), nullable=False)
    motivo = db.Column(db.Text)
    responsavel = db.Column(db.String(200))
    estado = db.Column(db.String(50))
    evidencia_esperada = db.Column(db.Text)
    bloqueia_implantacao = db.Column(db.String(80))

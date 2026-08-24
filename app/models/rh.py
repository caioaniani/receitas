"""Modelos do dominio: rh.

Faz parte de `app.models` (split em multiplos arquivos por dominio
em 2026-05-21). Importar via `from app.models import X` continua
funcionando porque `app/models/__init__.py` re-exporta tudo.
"""

from app.extensions import db
from app.utils import agora

funcionario_loja = db.Table('funcionario_loja',
    db.Column('funcionario_id', db.Integer, db.ForeignKey('funcionario.id'), primary_key=True),
    db.Column('loja_id', db.Integer, db.ForeignKey('loja.id'), primary_key=True),
    db.Column('loja_principal', db.Boolean, default=False),
)

class Cargo(db.Model):
    __tablename__ = 'cargo'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False, unique=True)
    salario_base = db.Column(db.Float, nullable=False, default=0)
    descricao = db.Column(db.String(200))
    ativo = db.Column(db.Boolean, default=True)
    criado_em = db.Column(db.DateTime, default=agora)

    def __repr__(self):
        return f'<Cargo {self.nome} R$ {self.salario_base:.2f}>'

class Funcionario(db.Model):
    __tablename__ = 'funcionario'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(200), nullable=False)
    cpf = db.Column(db.String(14), unique=True, nullable=False)
    funcao = db.Column(db.String(100))
    salario_base = db.Column(db.Float, default=0)
    data_admissao = db.Column(db.Date)
    data_demissao = db.Column(db.Date)
    ativo = db.Column(db.Boolean, default=True)
    cargo_id = db.Column(db.Integer, db.ForeignKey('cargo.id'), nullable=True)
    cargo_confianca = db.Column(db.Float, default=0)
    tem_cargo_confianca = db.Column(db.Boolean, default=False)
    hora_extra_pct = db.Column(db.Float, default=55)
    horas_extras = db.Column(db.Float, default=0)
    premiacao = db.Column(db.Float, default=0)
    vt_dia = db.Column(db.Float, default=0)
    vr_dia = db.Column(db.Float, default=22.00)
    dias_trabalhados = db.Column(db.Integer, default=26)
    telefone = db.Column(db.String(30))
    email = db.Column(db.String(150))
    observacao = db.Column(db.Text)
    funcao_operacional = db.Column(db.String(100))
    periodo = db.Column(db.String(20))
    cadastro_pendente = db.Column(db.Boolean, default=False)
    data_nascimento = db.Column(db.Date)
    # Portal do funcionário (24/07/2026): vínculo com a conta de login
    # (Usuario) por e-mail. NULL = funcionário ainda sem acesso. O ALTER já
    # está aplicado em prod (migrations_legacy, commit 1 confirmado pela sonda
    # /api/claude/deploy) ANTES deste modelo — procedimento de 2 commits.
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)
    # Liderança direta: uma pessoa tem no máximo um líder imediato; um líder
    # pode acompanhar várias pessoas. Quem possui liderados ativos ganha a
    # área "Minha equipe" no treinamento, independentemente do cargo técnico.
    lider_id = db.Column(db.Integer, db.ForeignKey('funcionario.id'),
                         nullable=True, index=True)

    lojas = db.relationship('Loja', secondary=funcionario_loja, backref='funcionarios')
    cargo = db.relationship('Cargo', backref='funcionarios')
    usuario = db.relationship('Usuario', foreign_keys=[usuario_id],
                              backref=db.backref('funcionario', uselist=False))
    lider = db.relationship(
        'Funcionario', remote_side=[id], foreign_keys=[lider_id],
        backref=db.backref('liderados', lazy='select'))

    def salario_efetivo(self):
        """Salario base vem do Cargo. Fallback para o campo legado se cargo nao setado."""
        try:
            if self.cargo:
                return self.cargo.salario_base or 0
        except Exception:
            pass
        return self.salario_base or 0

    def total_vt(self):
        return self.vt_dia * self.dias_trabalhados if self.vt_dia else 0

    def total_vr(self):
        return self.vr_dia * self.dias_trabalhados if self.vr_dia else 0

    def valor_cargo_confianca(self):
        """Cargo de confianca = 40% do salario efetivo (se ativo)."""
        try:
            if not getattr(self, 'tem_cargo_confianca', False):
                return 0
        except Exception:
            return 0
        return self.salario_efetivo() * 0.40

    def total_horas_extras(self):
        """Valor mensal das horas extras: (salario / 220h) * (1 + pct/100) * qtd_horas."""
        try:
            qtd = self.horas_extras or 0
        except Exception:
            return 0
        sal = self.salario_efetivo()
        if not qtd or not sal:
            return 0
        valor_hora = sal / 220.0
        adicional = 1 + (self.hora_extra_pct or 0) / 100
        return valor_hora * adicional * qtd

    def custo_total(self):
        return (
            self.salario_efetivo() +
            self.valor_cargo_confianca() +
            (self.premiacao or 0) +
            self.total_horas_extras() +
            self.total_vt() +
            self.total_vr()
        )

    def __repr__(self):
        return f'<Funcionario {self.nome}>'

class FolhaPagamento(db.Model):
    __tablename__ = 'folha_pagamento'
    __table_args__ = (
        db.UniqueConstraint('funcionario_id', 'mes', 'ano', name='uq_folha_func_mes_ano'),
    )

    id = db.Column(db.Integer, primary_key=True)
    funcionario_id = db.Column(db.Integer, db.ForeignKey('funcionario.id'), nullable=False)
    mes = db.Column(db.Integer, nullable=False)
    ano = db.Column(db.Integer, nullable=False)
    salario_base = db.Column(db.Float, default=0)
    cargo_confianca = db.Column(db.Float, default=0)
    horas_extras = db.Column(db.Float, default=0)
    premiacao = db.Column(db.Float, default=0)
    vt_dia = db.Column(db.Float, default=0)
    vr_dia = db.Column(db.Float, default=0)
    dias_trabalhados = db.Column(db.Integer, default=26)
    descontos = db.Column(db.Float, default=0)
    observacao = db.Column(db.Text)

    funcionario = db.relationship('Funcionario', backref='folhas')

    def total_vt(self):
        return self.vt_dia * self.dias_trabalhados

    def total_vr(self):
        return self.vr_dia * self.dias_trabalhados

    def total_bruto(self):
        return (
            self.salario_base +
            (self.cargo_confianca or 0) +
            (self.horas_extras or 0) +
            (self.premiacao or 0) +
            self.total_vt() +
            self.total_vr()
        )

    def total_liquido(self):
        return self.total_bruto() - (self.descontos or 0)

class Posicao(db.Model):
    __tablename__ = 'posicao'
    __table_args__ = (
        db.UniqueConstraint('loja_id', 'periodo', 'nome_posicao', name='uq_posicao_loja_periodo_nome'),
    )

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False)
    periodo = db.Column(db.String(20), nullable=False)
    nome_posicao = db.Column(db.String(100), nullable=False)
    funcionario_id = db.Column(db.Integer, db.ForeignKey('funcionario.id'), nullable=True)
    status = db.Column(db.String(30), default='ativo')
    observacao = db.Column(db.String(300))
    ordem = db.Column(db.Integer, default=0)
    origem = db.Column(db.String(10), default='manual')

    loja = db.relationship('Loja', backref='posicoes')
    funcionario = db.relationship('Funcionario', backref='posicoes')

    def __repr__(self):
        return f'<Posicao {self.nome_posicao} @ {self.loja_id}>'

class SlotMapa(db.Model):
    __tablename__ = 'slot_mapa'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False)
    nome = db.Column(db.String(100), nullable=False)
    pos_x = db.Column(db.Float, nullable=False)
    pos_y = db.Column(db.Float, nullable=False)
    largura = db.Column(db.Float, default=15)
    altura = db.Column(db.Float, default=8)

    loja = db.relationship('Loja', backref='slots')

    def __repr__(self):
        return f'<SlotMapa {self.nome} @ {self.loja_id}>'

class Feedback(db.Model):
    __tablename__ = 'feedback'

    id = db.Column(db.Integer, primary_key=True)
    funcionario_id = db.Column(db.Integer, db.ForeignKey('funcionario.id'), nullable=False)
    autor_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    data = db.Column(db.DateTime, default=agora)
    tipo = db.Column(db.String(20), default='neutro')
    texto = db.Column(db.Text, nullable=False)

    funcionario = db.relationship('Funcionario', backref='feedbacks')
    autor = db.relationship('Usuario', backref='feedbacks_dados')

    def __repr__(self):
        return f'<Feedback {self.funcionario_id} por {self.autor_id}>'

class Atestado(db.Model):
    __tablename__ = 'atestado'

    id = db.Column(db.Integer, primary_key=True)
    funcionario_id = db.Column(db.Integer, db.ForeignKey('funcionario.id'), nullable=False)
    data = db.Column(db.Date, nullable=False)
    motivo = db.Column(db.String(300))
    arquivo = db.Column(db.LargeBinary)
    arquivo_nome = db.Column(db.String(255))
    arquivo_mimetype = db.Column(db.String(100))
    criado_em = db.Column(db.DateTime, default=agora)
    criado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    funcionario = db.relationship('Funcionario', backref='atestados')
    autor = db.relationship('Usuario', backref='atestados_criados')

    def __repr__(self):
        return f'<Atestado {self.funcionario_id} em {self.data}>'

class Ferias(db.Model):
    __tablename__ = 'ferias'

    id = db.Column(db.Integer, primary_key=True)
    funcionario_id = db.Column(db.Integer, db.ForeignKey('funcionario.id'), nullable=False)
    data_inicio = db.Column(db.Date, nullable=False)
    data_fim = db.Column(db.Date, nullable=False)
    tipo = db.Column(db.String(20), default='ferias')
    status = db.Column(db.String(20), default='agendada')
    observacao = db.Column(db.Text)
    criado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    criado_em = db.Column(db.DateTime, default=agora)

    funcionario = db.relationship('Funcionario', backref='ferias')
    autor = db.relationship('Usuario', backref='ferias_criadas')

    def __repr__(self):
        return f'<Ferias {self.funcionario_id} {self.data_inicio}~{self.data_fim}>'

class RegistroPonto(db.Model):
    __tablename__ = 'registro_ponto'

    id = db.Column(db.Integer, primary_key=True)
    funcionario_id = db.Column(db.Integer, db.ForeignKey('funcionario.id'), nullable=False)
    data = db.Column(db.Date, nullable=False)
    entrada = db.Column(db.Time)
    saida = db.Column(db.Time)
    entrada2 = db.Column(db.Time)
    saida2 = db.Column(db.Time)
    horas_trabalhadas = db.Column(db.Float)
    horas_extras = db.Column(db.Float, default=0)
    observacao = db.Column(db.Text)
    editado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    __table_args__ = (
        db.UniqueConstraint('funcionario_id', 'data', name='uq_ponto_func_data'),
    )

    funcionario = db.relationship('Funcionario', backref='registros_ponto')

    def __repr__(self):
        return f'<Ponto {self.funcionario_id} {self.data}>'


class PreCadastroFuncionario(db.Model):
    """Pré-cadastro auto-serviço (23/07/2026): o funcionário preenche
    nome/sobrenome/e-mail/telefone num formulário aberto por QR Code. Vira uma
    linha aqui; o admin revisa no RH e PROMOVE pra `Funcionario` (informando o
    CPF que falta). Tabela nova via db.create_all (sem ALTER). Guarda PII —
    processados/antigos podem ser podados."""
    __tablename__ = 'pre_cadastro_funcionario'
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    sobrenome = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), nullable=False)
    telefone = db.Column(db.String(30), nullable=False)
    criado_em = db.Column(db.DateTime, default=agora, index=True)
    # Preenchidos quando o admin promove pra Funcionario (senão fica pendente).
    processado_em = db.Column(db.DateTime, nullable=True)
    funcionario_id = db.Column(db.Integer, db.ForeignKey('funcionario.id'),
                               nullable=True)

    funcionario = db.relationship('Funcionario')

    @property
    def nome_completo(self):
        return f'{self.nome} {self.sobrenome}'.strip()

    def __repr__(self):
        return f'<PreCadastroFuncionario {self.nome_completo}>'


# ── Estoque de Congelados (Produção) ──

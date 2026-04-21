from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from app.extensions import db


class Usuario(UserMixin, db.Model):
    __tablename__ = 'usuario'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    login = db.Column(db.String(50), nullable=False, unique=True)
    senha_hash = db.Column(db.String(256), nullable=False)
    papel = db.Column(db.String(20), nullable=False, default='funcionario')  # 'admin' ou 'funcionario'

    def set_senha(self, senha):
        self.senha_hash = generate_password_hash(senha, method='pbkdf2:sha256')

    def check_senha(self, senha):
        return check_password_hash(self.senha_hash, senha)

    def is_admin(self):
        return self.papel == 'admin'

    def __repr__(self):
        return f'<Usuario {self.login}>'


class Atribuicao(db.Model):
    __tablename__ = 'atribuicao'

    id = db.Column(db.Integer, primary_key=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False)
    status = db.Column(db.String(20), default='pendente')  # 'pendente' ou 'concluida'
    data_atribuicao = db.Column(db.DateTime, default=datetime.utcnow)
    data_conclusao = db.Column(db.DateTime)

    receita = db.relationship('Receita', backref='atribuicoes')
    usuario = db.relationship('Usuario', backref='atribuicoes')

    def __repr__(self):
        return f'<Atribuicao {self.receita_id} -> {self.usuario_id}>'


class MateriaPrima(db.Model):
    __tablename__ = 'materia_prima'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False, unique=True)
    unidade = db.Column(db.String(10), nullable=False, default='g')
    custo_por_kg = db.Column(db.Float, nullable=False)
    fornecedor = db.Column(db.String(100))
    observacoes = db.Column(db.String(200))

    def to_dict(self):
        return {
            'nome': self.nome,
            'unidade': self.unidade,
            'custo_por_kg': self.custo_por_kg,
            'fornecedor': self.fornecedor or '',
            'observacoes': self.observacoes or '',
        }

    estoque_atual = db.Column(db.Float, default=0)

    def __repr__(self):
        return f'<MateriaPrima {self.nome}>'


class Receita(db.Model):
    __tablename__ = 'receita'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False)
    categoria = db.Column(db.String(50))
    preco_venda = db.Column(db.Float)
    preco_loja = db.Column(db.Float)
    preco_site = db.Column(db.Float)
    rendimento_qtd = db.Column(db.Float, nullable=False)
    rendimento_unidade = db.Column(db.String(30), nullable=False)
    peso_base = db.Column(db.Float, nullable=False)
    peso_unitario = db.Column(db.Float)
    perda_percentual = db.Column(db.Float, default=0)
    custo_embalagem = db.Column(db.Float, default=0)
    modo_preparo = db.Column(db.Text)
    observacao = db.Column(db.Text)

    ingredientes = db.relationship(
        'ReceitaIngrediente',
        backref='receita',
        lazy=True,
        cascade='all, delete-orphan',
        order_by='ReceitaIngrediente.id'
    )

    def to_dict(self):
        return {
            'nome': self.nome,
            'categoria': self.categoria or '',
            'preco_venda': self.preco_venda,
            'preco_loja': self.preco_loja,
            'preco_site': self.preco_site,
            'rendimento_qtd': self.rendimento_qtd,
            'rendimento_unidade': self.rendimento_unidade,
            'peso_base': self.peso_base,
            'peso_unitario': self.peso_unitario,
            'perda_percentual': self.perda_percentual or 0,
            'custo_embalagem': self.custo_embalagem or 0,
            'modo_preparo': self.modo_preparo or '',
            'observacao': self.observacao or '',
            'ingredientes': [ing.to_dict() for ing in self.ingredientes],
        }

    def __repr__(self):
        return f'<Receita {self.nome}>'


class ReceitaIngrediente(db.Model):
    __tablename__ = 'receita_ingrediente'

    id = db.Column(db.Integer, primary_key=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=False)
    tipo = db.Column(db.String(10), default='mp')  # 'mp' ou 'receita'
    ingrediente_nome = db.Column(db.String(100), nullable=False)
    porcentagem = db.Column(db.Float, nullable=False)  # % padeiro (mp) ou qtd unidades (receita)
    eh_base = db.Column(db.Boolean, default=False)
    nota = db.Column(db.String(200))

    def to_dict(self):
        return {
            'tipo': self.tipo or 'mp',
            'ingrediente_nome': self.ingrediente_nome,
            'porcentagem': self.porcentagem,
            'eh_base': self.eh_base,
            'nota': self.nota or '',
        }

    def __repr__(self):
        return f'<ReceitaIngrediente {self.ingrediente_nome} {self.porcentagem}%>'


class Produto(db.Model):
    __tablename__ = 'produto'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False)
    categoria = db.Column(db.String(50))
    descricao = db.Column(db.String(300))
    preco_atacado = db.Column(db.Float)
    preco_loja = db.Column(db.Float)
    preco_site = db.Column(db.Float)
    custo_direto = db.Column(db.Float)  # custo por unidade para itens simples
    custo_embalagem = db.Column(db.Float, default=0)  # custo embalagem por unidade (R$)
    modo_preparo = db.Column(db.Text)
    observacao = db.Column(db.Text)
    ativo = db.Column(db.Boolean, default=True)

    itens = db.relationship(
        'ProdutoItem',
        backref='produto',
        lazy=True,
        cascade='all, delete-orphan',
        order_by='ProdutoItem.id'
    )

    def to_dict(self):
        return {
            'nome': self.nome,
            'categoria': self.categoria or '',
            'descricao': self.descricao or '',
            'preco_atacado': self.preco_atacado,
            'preco_loja': self.preco_loja,
            'preco_site': self.preco_site,
            'custo_direto': self.custo_direto,
            'custo_embalagem': self.custo_embalagem or 0,
            'modo_preparo': self.modo_preparo or '',
            'observacao': self.observacao or '',
            'ativo': self.ativo,
            'itens': [item.to_dict() for item in self.itens],
        }

    def __repr__(self):
        return f'<Produto {self.nome}>'


class Loja(db.Model):
    __tablename__ = 'loja'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False, unique=True)
    endereco = db.Column(db.String(300))
    telefone = db.Column(db.String(30))
    ativa = db.Column(db.Boolean, default=True)
    planta_imagem = db.Column(db.LargeBinary)
    planta_mimetype = db.Column(db.String(100))

    def __repr__(self):
        return f'<Loja {self.nome}>'


funcionario_loja = db.Table('funcionario_loja',
    db.Column('funcionario_id', db.Integer, db.ForeignKey('funcionario.id'), primary_key=True),
    db.Column('loja_id', db.Integer, db.ForeignKey('loja.id'), primary_key=True),
    db.Column('loja_principal', db.Boolean, default=False),
)


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
    cargo_confianca = db.Column(db.Float, default=0)
    hora_extra_pct = db.Column(db.Float, default=55)
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

    lojas = db.relationship('Loja', secondary=funcionario_loja, backref='funcionarios')

    def total_vt(self):
        return self.vt_dia * self.dias_trabalhados if self.vt_dia else 0

    def total_vr(self):
        return self.vr_dia * self.dias_trabalhados if self.vr_dia else 0

    def custo_total(self):
        return (
            self.salario_base +
            (self.cargo_confianca or 0) +
            (self.premiacao or 0) +
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
    data = db.Column(db.DateTime, default=datetime.utcnow)
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
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    criado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    funcionario = db.relationship('Funcionario', backref='atestados')
    autor = db.relationship('Usuario', backref='atestados_criados')

    def __repr__(self):
        return f'<Atestado {self.funcionario_id} em {self.data}>'


class ProdutoItem(db.Model):
    __tablename__ = 'produto_item'

    id = db.Column(db.Integer, primary_key=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=False)
    tipo = db.Column(db.String(10), nullable=False)  # 'receita' ou 'mp'
    item_nome = db.Column(db.String(150), nullable=False)
    quantidade = db.Column(db.Float, nullable=False, default=1)

    def to_dict(self):
        return {
            'tipo': self.tipo,
            'item_nome': self.item_nome,
            'quantidade': self.quantidade,
        }

    def __repr__(self):
        return f'<ProdutoItem {self.item_nome} x{self.quantidade}>'


class MovimentacaoEstoque(db.Model):
    __tablename__ = 'movimentacao_estoque'

    id = db.Column(db.Integer, primary_key=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'), nullable=False)
    tipo = db.Column(db.String(10), nullable=False)  # 'entrada' ou 'saida'
    quantidade = db.Column(db.Float, nullable=False)
    preco_unitario = db.Column(db.Float)
    data = db.Column(db.DateTime, default=datetime.utcnow)
    referencia = db.Column(db.String(200))
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    materia_prima = db.relationship('MateriaPrima', backref='movimentacoes')
    usuario = db.relationship('Usuario', backref='movimentacoes_estoque')

    def __repr__(self):
        return f'<Movimentacao {self.tipo} {self.quantidade} MP={self.materia_prima_id}>'


class AlertaEstoque(db.Model):
    __tablename__ = 'alerta_estoque'

    id = db.Column(db.Integer, primary_key=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'), nullable=False, unique=True)
    estoque_minimo = db.Column(db.Float, nullable=False)

    materia_prima = db.relationship('MateriaPrima', backref='alerta_estoque', uselist=False)

    def __repr__(self):
        return f'<AlertaEstoque MP={self.materia_prima_id} min={self.estoque_minimo}>'


class PlanejamentoProducao(db.Model):
    __tablename__ = 'planejamento_producao'

    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.Date, nullable=False)
    nome = db.Column(db.String(100))
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    criado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    status = db.Column(db.String(20), default='rascunho')

    itens = db.relationship('PlanejamentoItem', backref='planejamento',
                            cascade='all, delete-orphan', lazy=True)
    autor = db.relationship('Usuario', backref='planejamentos')

    def __repr__(self):
        return f'<Planejamento {self.nome} em {self.data}>'


class PlanejamentoItem(db.Model):
    __tablename__ = 'planejamento_item'

    id = db.Column(db.Integer, primary_key=True)
    planejamento_id = db.Column(db.Integer, db.ForeignKey('planejamento_producao.id'), nullable=False)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=False)
    multiplicador = db.Column(db.Integer, default=1)

    receita = db.relationship('Receita')

    def __repr__(self):
        return f'<PlanejamentoItem receita={self.receita_id} x{self.multiplicador}>'


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
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)

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

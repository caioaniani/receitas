from datetime import datetime, date

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from app.extensions import db
from app.utils.time import agora


class Usuario(UserMixin, db.Model):
    __tablename__ = 'usuario'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    login = db.Column(db.String(50), nullable=False, unique=True)
    senha_hash = db.Column(db.String(256), nullable=False)
    papel = db.Column(db.String(20), nullable=False, default='funcionario')
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=True)
    is_owner = db.Column(db.Boolean, default=False)

    loja = db.relationship('Loja', backref='usuarios')

    def set_senha(self, senha):
        self.senha_hash = generate_password_hash(senha, method='pbkdf2:sha256')

    def check_senha(self, senha):
        return check_password_hash(self.senha_hash, senha)

    def is_admin(self):
        return self.papel == 'admin' or self.is_dono()

    def is_gerente(self):
        return self.papel == 'gerente'

    def is_producao(self):
        return self.papel == 'producao'

    def is_rh(self):
        return self.papel == 'rh'

    def pode_lojas(self):
        """Pedidos, Estoque Loja, Relatorio."""
        return self.is_admin() or self.is_gerente()

    def pode_producao(self):
        """Plano de Producao, Congelados, Separacao."""
        return self.is_admin() or self.is_producao()

    def pode_catalogo(self):
        """Receitas, MP, Produtos, Fornecedores (producao = read-only)."""
        return self.is_admin() or self.is_producao()

    def pode_rh(self):
        """RH (ponto/ferias/cargos sem salario)."""
        return self.is_admin() or self.is_rh()

    def pode_pdv(self):
        """PDV, Seru, VNDA, Mapeamentos."""
        return self.is_admin()

    def is_dono(self):
        """Owner: dono unico do sistema (ve areas pessoais Vida/Igreja).
        Defensivo: se a coluna ainda nao existir, retorna False sem quebrar."""
        try:
            return bool(getattr(self, 'is_owner', False))
        except Exception:
            return False

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
    peso_unidade = db.Column(db.Float, nullable=True)
    fornecedor = db.Column(db.String(100))
    observacoes = db.Column(db.String(200))

    def to_dict(self):
        return {
            'nome': self.nome,
            'unidade': self.unidade,
            'custo_por_kg': self.custo_por_kg,
            'peso_unidade': self.peso_unidade,
            'fornecedor': self.fornecedor or '',
            'observacoes': self.observacoes or '',
        }

    estoque_atual = db.Column(db.Float, default=0)

    def __repr__(self):
        return f'<MateriaPrima {self.nome}>'


class Fornecedor(db.Model):
    """Fornecedor de materias-primas. Histórico de compras + cadastro
    pra evitar texto solto em MateriaPrima.fornecedor (campo legacy)."""
    __tablename__ = 'fornecedor'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False, unique=True)
    cnpj = db.Column(db.String(20))
    telefone = db.Column(db.String(30))
    email = db.Column(db.String(120))
    contato = db.Column(db.String(100))  # nome do contato/vendedor
    observacao = db.Column(db.Text)
    ativo = db.Column(db.Boolean, default=True, index=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)


class HistoricoPrecoMP(db.Model):
    """Registro de cada compra de MP de um fornecedor — preço, quantidade,
    data. Alimentado automaticamente quando ha entrada de MP via
    MovimentacaoEstoque com fornecedor_id."""
    __tablename__ = 'historico_preco_mp'

    id = db.Column(db.Integer, primary_key=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'), nullable=False, index=True)
    fornecedor_id = db.Column(db.Integer, db.ForeignKey('fornecedor.id'), nullable=False, index=True)
    preco_unitario = db.Column(db.Float, nullable=False)
    quantidade = db.Column(db.Float, nullable=False)
    data = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    referencia = db.Column(db.String(200))  # NF, observacao
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    materia_prima = db.relationship('MateriaPrima')
    fornecedor = db.relationship('Fornecedor', backref='compras')
    usuario = db.relationship('Usuario')


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


class Cargo(db.Model):
    __tablename__ = 'cargo'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False, unique=True)
    salario_base = db.Column(db.Float, nullable=False, default=0)
    descricao = db.Column(db.String(200))
    ativo = db.Column(db.Boolean, default=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)

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

    lojas = db.relationship('Loja', secondary=funcionario_loja, backref='funcionarios')
    cargo = db.relationship('Cargo', backref='funcionarios')

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
    # Fornecedor (opcional) — usado em entradas pra alimentar historico de preco
    fornecedor_id = db.Column(db.Integer, db.ForeignKey('fornecedor.id'))

    materia_prima = db.relationship('MateriaPrima', backref='movimentacoes')
    usuario = db.relationship('Usuario', backref='movimentacoes_estoque')
    fornecedor = db.relationship('Fornecedor')

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


# ── Estoque de Congelados (Produção) ──

class EstoqueProducao(db.Model):
    __tablename__ = 'estoque_producao'

    id = db.Column(db.Integer, primary_key=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)
    quantidade = db.Column(db.Integer, default=0)
    # Nome digitado no balanco quando nao houve match com Receita/Produto.
    # Permite registrar a contagem fisica mesmo sem cadastro previo;
    # depois o admin vincula a uma receita/produto e isso volta a NULL.
    nome_pendente = db.Column(db.String(200), nullable=True)

    receita = db.relationship('Receita')
    produto = db.relationship('Produto')
    movimentacoes = db.relationship('MovEstoqueProducao', backref='estoque', cascade='all, delete-orphan')

    @property
    def nome_item(self):
        if self.receita:
            return self.receita.nome
        if self.produto:
            return self.produto.nome
        if self.nome_pendente:
            return self.nome_pendente
        return '?'

    @property
    def pendente(self):
        return self.receita_id is None and self.produto_id is None and bool(self.nome_pendente)


class MovEstoqueProducao(db.Model):
    __tablename__ = 'mov_estoque_producao'

    id = db.Column(db.Integer, primary_key=True)
    estoque_producao_id = db.Column(db.Integer, db.ForeignKey('estoque_producao.id'), nullable=False)
    tipo = db.Column(db.String(20), nullable=False)
    quantidade = db.Column(db.Integer, nullable=False)
    data = db.Column(db.DateTime, default=datetime.utcnow)
    referencia = db.Column(db.String(200))
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))


# ── Pedidos de Loja ──

class PedidoLoja(db.Model):
    __tablename__ = 'pedido_loja'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False)
    data_pedido = db.Column(db.Date, default=date.today)
    data_entrega = db.Column(db.Date)
    status = db.Column(db.String(20), default='pendente')
    observacao = db.Column(db.Text)
    criado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)

    loja = db.relationship('Loja', backref='pedidos')
    criador = db.relationship('Usuario')
    itens = db.relationship('PedidoItem', backref='pedido', cascade='all, delete-orphan')

    @property
    def tem_divergencia(self):
        return any(
            i.quantidade_recebida is not None and i.quantidade_recebida != i.quantidade
            for i in self.itens
        )

    @property
    def itens_divergentes(self):
        return [
            i for i in self.itens
            if i.quantidade_recebida is not None and i.quantidade_recebida != i.quantidade
        ]


class PedidoItem(db.Model):
    __tablename__ = 'pedido_item'

    id = db.Column(db.Integer, primary_key=True)
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedido_loja.id'), nullable=False)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'), nullable=True)
    quantidade = db.Column(db.Integer, nullable=False)
    quantidade_recebida = db.Column(db.Integer, nullable=True)
    observacao = db.Column(db.String(200))

    receita = db.relationship('Receita')
    produto = db.relationship('Produto')
    materia_prima = db.relationship('MateriaPrima')

    @property
    def nome_item(self):
        if self.receita:
            return self.receita.nome
        if self.produto:
            return self.produto.nome
        if self.materia_prima:
            return self.materia_prima.nome + ' (MP)'
        return '?'


# ── Estoque de Loja ──

class EstoqueLoja(db.Model):
    __tablename__ = 'estoque_loja'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'), nullable=True)
    quantidade = db.Column(db.Integer, default=0)
    # Nome digitado em entrada-em-lote quando nao houve match com nenhum
    # cadastro. Mesma logica do EstoqueProducao.nome_pendente.
    nome_pendente = db.Column(db.String(200), nullable=True)

    loja = db.relationship('Loja')
    receita = db.relationship('Receita')
    produto = db.relationship('Produto')
    materia_prima = db.relationship('MateriaPrima')
    movimentacoes = db.relationship('MovEstoqueLoja', backref='estoque', cascade='all, delete-orphan')

    @property
    def nome_item(self):
        if self.receita:
            return self.receita.nome
        if self.produto:
            return self.produto.nome
        if self.materia_prima:
            return self.materia_prima.nome + ' (MP)'
        if self.nome_pendente:
            return self.nome_pendente
        return '?'

    @property
    def pendente(self):
        return (self.receita_id is None and self.produto_id is None
                and self.materia_prima_id is None and bool(self.nome_pendente))


class PrecoLojaReceita(db.Model):
    __tablename__ = 'preco_loja_receita'

    id = db.Column(db.Integer, primary_key=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=False)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=False)
    preco = db.Column(db.Float, nullable=False)

    __table_args__ = (db.UniqueConstraint('loja_id', 'receita_id', name='uq_preco_loja_receita'),)


class FotoRecebimento(db.Model):
    __tablename__ = 'foto_recebimento'

    id = db.Column(db.Integer, primary_key=True)
    pedido_id = db.Column(db.Integer, db.ForeignKey('pedido_loja.id'), nullable=False)
    imagem = db.Column(db.LargeBinary, nullable=False)
    mimetype = db.Column(db.String(100))
    enviada_em = db.Column(db.DateTime, default=datetime.utcnow)
    enviada_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    pedido = db.relationship('PedidoLoja', backref=db.backref('fotos', cascade='all, delete-orphan'))


class MovEstoqueLoja(db.Model):
    __tablename__ = 'mov_estoque_loja'

    id = db.Column(db.Integer, primary_key=True)
    estoque_loja_id = db.Column(db.Integer, db.ForeignKey('estoque_loja.id'), nullable=False)
    # 50 pra caber 'venda_seru_sem_estoque' (22) e futuros tipos.
    tipo = db.Column(db.String(50), nullable=False)
    quantidade = db.Column(db.Integer, nullable=False)
    data = db.Column(db.DateTime, default=datetime.utcnow)
    referencia = db.Column(db.String(200))
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'))


# ── Cartinha de Entrega (Vnda) ──

class CartinhaEntrega(db.Model):
    __tablename__ = 'cartinha_entrega'

    id = db.Column(db.Integer, primary_key=True)
    pedido_code = db.Column(db.String(50), nullable=False, unique=True)
    texto = db.Column(db.Text)
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow)
    atualizado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    autor = db.relationship('Usuario', backref='cartinhas')


class OverrideEntrega(db.Model):
    """Sobrescreve a data de entrega de um pedido VNDA — local, nao sincroniza com o VNDA."""
    __tablename__ = 'override_entrega'

    id = db.Column(db.Integer, primary_key=True)
    pedido_code = db.Column(db.String(50), nullable=False, unique=True, index=True)
    data_entrega = db.Column(db.Date, nullable=False)
    motivo = db.Column(db.Text)
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow)
    atualizado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    autor = db.relationship('Usuario', backref='overrides_entrega')


class GeocodeCache(db.Model):
    """Cache de enderecos geocodificados (CEP -> lat/lng).
    Evita re-bater o Nominatim (rate limit 1 req/s)."""
    __tablename__ = 'geocode_cache'

    id = db.Column(db.Integer, primary_key=True)
    chave = db.Column(db.String(200), nullable=False, unique=True, index=True)
    lat = db.Column(db.Float)
    lng = db.Column(db.Float)
    fonte = db.Column(db.String(50))  # 'brasilapi', 'awesomeapi', 'nominatim', 'nominatim_cep_rejeitado', etc.
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)


class Driver(db.Model):
    """Motorista/motoboy cadastrado. Pedidos sao atribuidos a um Driver."""
    __tablename__ = 'driver_entrega'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(80), nullable=False, unique=True)
    cor = db.Column(db.String(20))  # opcional: hex pra UI
    telefone = db.Column(db.String(30))
    ativo = db.Column(db.Boolean, default=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    # Capacidade maxima de pedidos por rodada de Auto-distribuir.
    # Usada pra moto (cap 2-3) vs carro (cap 12-15). Default alto = sem limite efetivo.
    capacidade = db.Column(db.Integer, default=999)

    # Acesso a pagina /driver/<token> + PIN 4 digitos pra dificultar acesso casual.
    token = db.Column(db.String(32), unique=True, index=True)
    pin = db.Column(db.String(8))  # 4 digitos, mas folga pra futuros 6

    atribuicoes = db.relationship('AtribuicaoEntrega', backref='driver', lazy='dynamic')


class LoteSaida(db.Model):
    """Pacote nomeado de uma rodada de distribuicao.
    Cada vez que o usuario clica 'Distribuir' (ou cria manualmente), gera 1 lote.
    Status e inferido a partir das atribuicoes filhas."""
    __tablename__ = 'lote_saida'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    data_entrega = db.Column(db.Date, nullable=False, index=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    janelas_json = db.Column(db.Text)  # JSON array de strings, ex: ["07-08","08-09"]
    # aberto = nenhum saiu | em_rota = >=1 saiu, falta entregar | concluido = 100%
    status = db.Column(db.String(20), default='aberto', index=True)
    criado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))


class AtribuicaoEntrega(db.Model):
    """Vincula um pedido VNDA a um Driver. Pedido tem no maximo 1 driver por vez."""
    __tablename__ = 'atribuicao_entrega'

    id = db.Column(db.Integer, primary_key=True)
    pedido_code = db.Column(db.String(50), nullable=False, unique=True, index=True)
    driver_id = db.Column(db.Integer, db.ForeignKey('driver_entrega.id'))
    lote_id = db.Column(db.Integer, db.ForeignKey('lote_saida.id'), index=True)
    data_entrega = db.Column(db.Date, index=True)
    ordem = db.Column(db.Integer, default=0)  # ordem dentro da rota do driver
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    atualizado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    # Status preenchido pela pagina do driver
    status = db.Column(db.String(20), default='pendente')  # pendente|entregue|nao_entregue
    entregue_em = db.Column(db.DateTime)
    nota = db.Column(db.String(500))
    motivo_falha = db.Column(db.String(50))  # ausente|recusou|endereco_errado|outro
    geo_lat = db.Column(db.Float)
    geo_lng = db.Column(db.Float)
    # Hash publico pra link compartilhavel com cliente
    proof_hash = db.Column(db.String(32), unique=True, index=True)

    autor = db.relationship('Usuario')
    fotos = db.relationship('EntregaFoto', backref='atribuicao', lazy='dynamic',
                            cascade='all, delete-orphan')


class EntregaFoto(db.Model):
    """Foto de comprovante de entrega tirada pelo driver."""
    __tablename__ = 'entrega_foto'

    id = db.Column(db.Integer, primary_key=True)
    atribuicao_id = db.Column(db.Integer, db.ForeignKey('atribuicao_entrega.id'), nullable=False, index=True)
    url = db.Column(db.String(500), nullable=False)  # URL publica (Dropbox shared link)
    storage_path = db.Column(db.String(500))  # caminho no storage pra deletar depois
    tirada_em = db.Column(db.DateTime, default=datetime.utcnow)
    tamanho_bytes = db.Column(db.Integer)


class PedidoLocal(db.Model):
    """Pedido cadastrado manualmente, fora do VNDA. Aparece junto com os
    pedidos VNDA na operacao do dia."""
    __tablename__ = 'pedido_local'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False, index=True)
    destinatario = db.Column(db.String(200), nullable=False)
    telefone = db.Column(db.String(50), nullable=False)
    endereco = db.Column(db.String(500), nullable=False)
    data_entrega = db.Column(db.Date, nullable=False, index=True)
    periodo = db.Column(db.String(80))
    cartinha = db.Column(db.Text)
    observacao = db.Column(db.Text)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    criado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'))

    itens = db.relationship('PedidoLocalItem', backref='pedido', cascade='all, delete-orphan', lazy='joined')

    @property
    def total(self):
        return sum((i.quantidade or 0) * (i.preco_unitario or 0) for i in self.itens)


class PedidoLocalItem(db.Model):
    __tablename__ = 'pedido_local_item'

    id = db.Column(db.Integer, primary_key=True)
    pedido_local_id = db.Column(db.Integer, db.ForeignKey('pedido_local.id', ondelete='CASCADE'), nullable=False, index=True)
    nome = db.Column(db.String(200), nullable=False)
    quantidade = db.Column(db.Integer, default=1)
    preco_unitario = db.Column(db.Float, default=0)


class CopilotConversa(db.Model):
    """Audit trail das interacoes com o copilot.
    Cada prompt do usuario vira 1 registro. Guarda a interpretacao da
    LLM, status (pendente/aprovado/cancelado/executado/falhou) e link
    pro registro resultante (ex: pedido criado)."""
    __tablename__ = 'copilot_conversa'

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=False, index=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    prompt = db.Column(db.Text, nullable=False)
    # JSON com {tipo, params, explicacao, ambiguidades?}
    interpretacao_json = db.Column(db.Text)
    tipo_acao = db.Column(db.String(40), index=True)
    status = db.Column(db.String(20), default='pendente', index=True)
    executado_em = db.Column(db.DateTime)
    # Link pro registro criado (ex: pedido_loja.id se criou um pedido)
    registro_tipo = db.Column(db.String(40))
    registro_id = db.Column(db.Integer)
    erro = db.Column(db.Text)

    usuario = db.relationship('Usuario')


# ── Gestao de Projetos (PARA + 12 Week Year) ──

class AuditLog(db.Model):
    """Trilha de auditoria de mutacoes em modelos sensiveis.
    Populado automaticamente via SQLAlchemy event listener (depois_flush)
    pros modelos registrados em audit_models.py.

    Guarda snapshot 'antes' e 'depois' em JSON pra reconstrucao."""
    __tablename__ = 'audit_log'

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuario.id'), index=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    tabela = db.Column(db.String(60), nullable=False, index=True)
    registro_id = db.Column(db.Integer, index=True)
    acao = db.Column(db.String(10), nullable=False)  # insert | update | delete
    antes = db.Column(db.Text)  # JSON: snapshot pré-mudança (null em insert)
    depois = db.Column(db.Text)  # JSON: snapshot pós-mudança (null em delete)
    ip = db.Column(db.String(45))
    user_agent = db.Column(db.String(300))

    usuario = db.relationship('Usuario')


class ProjetoArea(db.Model):
    __tablename__ = "projeto_area"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False, unique=True)
    tipo = db.Column(db.String(20), nullable=False, default="empresa")  # empresa/igreja/vida
    cor = db.Column(db.String(20))  # ex: '#5b8def' — opcional, sobrescreve cor padrao do tipo
    ativa = db.Column(db.Boolean, default=True)
    ordem = db.Column(db.Integer, default=0)

    projetos = db.relationship("Projeto", backref="area",
                                cascade="all, delete-orphan",
                                order_by="Projeto.criado_em.desc()")


class Projeto(db.Model):
    __tablename__ = "projeto"

    id = db.Column(db.Integer, primary_key=True)
    area_id = db.Column(db.Integer, db.ForeignKey("projeto_area.id"), nullable=False)
    nome = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(20), default="planejado")
    prioridade = db.Column(db.String(10))
    foco_12s = db.Column(db.Boolean, default=False)
    responsavel_id = db.Column(db.Integer, db.ForeignKey("usuario.id"), nullable=True)
    observacao = db.Column(db.Text)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    responsavel = db.relationship("Usuario")
    tarefas = db.relationship("TarefaProjeto", backref="projeto",
                               cascade="all, delete-orphan",
                               order_by="TarefaProjeto.ordem, TarefaProjeto.id")

    @property
    def tarefas_ativas(self):
        return [t for t in self.tarefas if t.status not in ("feito", "cancelado")]

    @property
    def tem_atrasada(self):
        return any(t.atrasada for t in self.tarefas)


class TarefaProjeto(db.Model):
    __tablename__ = "tarefa_projeto"

    id = db.Column(db.Integer, primary_key=True)
    projeto_id = db.Column(db.Integer, db.ForeignKey("projeto.id"), nullable=False)
    nome = db.Column(db.String(300), nullable=False)
    status = db.Column(db.String(20), default="a_fazer")
    tipo = db.Column(db.String(20))
    esforco = db.Column(db.String(2))
    prazo = db.Column(db.Date, nullable=True)
    responsavel_id = db.Column(db.Integer, db.ForeignKey("usuario.id"), nullable=True)
    observacao = db.Column(db.Text)
    recorrencia = db.Column(db.String(20))  # diaria/semanal/quinzenal/mensal/trimestral
    ordem = db.Column(db.Integer, default=0)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    feito_em = db.Column(db.DateTime, nullable=True)

    responsavel = db.relationship("Usuario")

    @property
    def atrasada(self):
        return (self.prazo is not None
                and self.status not in ("feito", "cancelado")
                and self.prazo < date.today())



class WeeklyReview(db.Model):
    __tablename__ = "weekly_review"

    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.Date, default=date.today, nullable=False)
    reflexao = db.Column(db.Text)
    fazendo_count = db.Column(db.Integer, default=0)
    a_fazer_count = db.Column(db.Integer, default=0)
    atrasadas_count = db.Column(db.Integer, default=0)
    foco_count = db.Column(db.Integer, default=0)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    criado_por = db.Column(db.Integer, db.ForeignKey("usuario.id"))

    autor = db.relationship("Usuario")


# ── Templates de Projeto ──

class ProjetoTemplate(db.Model):
    __tablename__ = "projeto_template"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False)
    area_id_padrao = db.Column(db.Integer, db.ForeignKey("projeto_area.id"), nullable=True)
    descricao = db.Column(db.Text)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)

    area_padrao = db.relationship("ProjetoArea")
    tarefas = db.relationship("TarefaTemplate", backref="template",
                               cascade="all, delete-orphan",
                               order_by="TarefaTemplate.ordem")


class TarefaTemplate(db.Model):
    __tablename__ = "tarefa_template"

    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey("projeto_template.id"), nullable=False)
    nome = db.Column(db.String(300), nullable=False)
    tipo = db.Column(db.String(20))
    esforco = db.Column(db.String(2))
    dias_prazo = db.Column(db.Integer)  # dias a partir da criacao do projeto
    ordem = db.Column(db.Integer, default=0)


# ── Integracao Seru (PDV): mapeamento de produtos/lojas + idempotencia ──

class SeruProdutoMap(db.Model):
    """Mapeia 'nome do produto' como vem da Seru pra um item do nosso catalogo.

    Estados (mutuamente exclusivos):
    - MAPEADO: receita_id ou produto_id setado → auto-baixa estoque na venda
    - IGNORADO: ignorar=True → nunca processa (cafe, agua, etc)
    - PENDENTE: tudo NULL/False → fica na fila de revisao, vendas nao baixam

    Composicao: fator_quantidade indica quanto 1 venda Seru desconta do alvo.
    Ex: 'NOZES COM MANTEIGA' = 2 fatias de 1 Sourdough que rende 10 fatias →
    fator_quantidade = 0.2. Default 1.0 (1 venda = 1 unidade do alvo).
    """
    __tablename__ = 'seru_produto_map'

    id = db.Column(db.Integer, primary_key=True)
    seru_nome = db.Column(db.String(300), nullable=False, unique=True, index=True)
    seru_sku = db.Column(db.String(100), nullable=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)
    ignorar = db.Column(db.Boolean, default=False, nullable=False)
    fator_quantidade = db.Column(db.Float, nullable=False, default=1.0)

    primeira_visto_em = db.Column(db.DateTime, default=datetime.utcnow)
    confirmado_em = db.Column(db.DateTime, nullable=True)
    confirmado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)

    receita = db.relationship('Receita')
    produto = db.relationship('Produto')

    @property
    def estado(self):
        if self.ignorar:
            return 'ignorado'
        if self.receita_id or self.produto_id:
            return 'mapeado'
        return 'pendente'

    @property
    def alvo_nome(self):
        if self.receita:
            return self.receita.nome
        if self.produto:
            return self.produto.nome
        return None


class SeruLojaMap(db.Model):
    """Mapeia 'company.name' da Seru pra nossa Loja. Auto-fuzzy na primeira
    aparicao; admin pode confirmar/corrigir/ignorar via /pdv/config-lojas."""
    __tablename__ = 'seru_loja_map'

    id = db.Column(db.Integer, primary_key=True)
    seru_company_name = db.Column(db.String(300), nullable=False, unique=True, index=True)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=True)
    ignorar = db.Column(db.Boolean, default=False, nullable=False)
    auto_match = db.Column(db.Boolean, default=False)  # True se foi setado via fuzzy
    confirmado_em = db.Column(db.DateTime, nullable=True)
    confirmado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)

    loja = db.relationship('Loja')

    @property
    def estado(self):
        if self.ignorar:
            return 'ignorado'
        if self.loja_id:
            return 'mapeado'
        return 'pendente'


class SeruPedidoProcessado(db.Model):
    """Garante idempotencia: cada pedido Seru e processado UMA vez.
    Se a venda for cancelada na Seru depois, marcamos cancelado_em e
    o proximo sync gera estornos."""
    __tablename__ = 'seru_pedido_processado'

    seru_pedido_id = db.Column(db.String(100), primary_key=True)
    processado_em = db.Column(db.DateTime, default=datetime.utcnow)
    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), nullable=True)
    n_itens_total = db.Column(db.Integer, default=0)
    n_itens_baixados = db.Column(db.Integer, default=0)
    cancelado_em = db.Column(db.DateTime, nullable=True)
    estornado_em = db.Column(db.DateTime, nullable=True)


class SeruDebito(db.Model):
    """Acumulador de baixas fracionadas por (loja, produto Seru).

    Quando um produto Seru tem fator_quantidade < 1 (ex: 0.2), vender 1 nao
    baixa estoque inteiro. A fracao fica aqui ate atingir >= 1 inteiro, dai
    baixa N inteiros do EstoqueLoja e fracao_pendente fica com o resto.
    """
    __tablename__ = 'seru_debito'

    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), primary_key=True)
    seru_produto_map_id = db.Column(db.Integer,
                                     db.ForeignKey('seru_produto_map.id', ondelete='CASCADE'),
                                     primary_key=True)
    fracao_pendente = db.Column(db.Float, nullable=False, default=0.0)
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow,
                               onupdate=datetime.utcnow)


# ── Integracao VNDA (site/e-commerce): mapeamentos + idempotencia ──
# Sempre baixa da loja fixa (Loja Anesio Pinto Rosa). Baixa acontece no
# dia da entrega (expected_delivery_date), nao quando pago/entregue.

class VndaProdutoMap(db.Model):
    """Espelha SeruProdutoMap — mesma logica de estado e fator."""
    __tablename__ = 'vnda_produto_map'

    id = db.Column(db.Integer, primary_key=True)
    vnda_nome = db.Column(db.String(300), nullable=False, unique=True, index=True)
    vnda_sku = db.Column(db.String(100), nullable=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)
    ignorar = db.Column(db.Boolean, default=False, nullable=False)
    fator_quantidade = db.Column(db.Float, nullable=False, default=1.0)

    primeira_visto_em = db.Column(db.DateTime, default=datetime.utcnow)
    confirmado_em = db.Column(db.DateTime, nullable=True)
    confirmado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)

    receita = db.relationship('Receita')
    produto = db.relationship('Produto')

    @property
    def estado(self):
        if self.ignorar:
            return 'ignorado'
        if self.receita_id or self.produto_id:
            return 'mapeado'
        return 'pendente'

    @property
    def alvo_nome(self):
        if self.receita:
            return self.receita.nome
        if self.produto:
            return self.produto.nome
        return None


class VndaPedidoProcessado(db.Model):
    """Idempotencia: cada pedido VNDA processado uma vez. Identificado pelo
    'code' do VNDA. Cancelados depois geram estorno automatico."""
    __tablename__ = 'vnda_pedido_processado'

    vnda_pedido_code = db.Column(db.String(100), primary_key=True)
    processado_em = db.Column(db.DateTime, default=datetime.utcnow)
    data_entrega = db.Column(db.Date)  # data agendada de entrega
    n_itens_total = db.Column(db.Integer, default=0)
    n_itens_baixados = db.Column(db.Integer, default=0)
    cancelado_em = db.Column(db.DateTime, nullable=True)
    estornado_em = db.Column(db.DateTime, nullable=True)


class AppConfig(db.Model):
    """Key-value generico pra configuracoes runtime (sem precisar de
    redeploy/env var). Use AppConfig.get(k, default) e AppConfig.set(k, v)."""
    __tablename__ = 'app_config'

    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text)

    @classmethod
    def get(cls, key, default=None):
        row = cls.query.filter_by(key=key).first()
        return row.value if row else default

    @classmethod
    def get_int(cls, key, default=None):
        v = cls.get(key)
        if v is None:
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    @classmethod
    def set(cls, key, value):
        row = cls.query.filter_by(key=key).first()
        v = str(value) if value is not None else None
        if row:
            row.value = v
        else:
            row = cls(key=key, value=v)
            db.session.add(row)
        return row


class VndaDebito(db.Model):
    """Acumulador de baixas fracionadas por produto VNDA + componente.

    `componente_key` permite que CESTAS (Produto com ProdutoItens) tenham
    um acumulador POR COMPONENTE — cada item interno baixa separado.
    Valores: 'self' (produto simples) | 'r:<id>' (receita componente) |
    'm:<id>' (materia-prima componente).
    """
    __tablename__ = 'vnda_debito'

    vnda_produto_map_id = db.Column(db.Integer,
                                     db.ForeignKey('vnda_produto_map.id', ondelete='CASCADE'),
                                     primary_key=True)
    componente_key = db.Column(db.String(50), primary_key=True, default='self')
    fracao_pendente = db.Column(db.Float, nullable=False, default=0.0)
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow,
                               onupdate=datetime.utcnow)


# ── Saida em lote manual (lojas com PDV sem API) ──
# Mapeia nomes digitados em /pedidos/estoque-loja/saida-lote pra catalogo.
# Vincular uma vez, lembra pra sempre. Espelha SeruProdutoMap.

class LojaProdutoMap(db.Model):
    """Mapeamento persistente de nomes digitados (saida em lote) → catalogo.

    Estados:
    - MAPEADO: receita_id/produto_id/materia_prima_id setado → baixa
    - IGNORADO: ignorar=True → nunca desconta
    - PENDENTE: nada vinculado → fica na fila, saidas nao mexem em estoque
    """
    __tablename__ = 'loja_produto_map'

    id = db.Column(db.Integer, primary_key=True)
    nome_digitado = db.Column(db.String(200), nullable=False, unique=True, index=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=True)
    produto_id = db.Column(db.Integer, db.ForeignKey('produto.id'), nullable=True)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'), nullable=True)
    ignorar = db.Column(db.Boolean, default=False, nullable=False)
    fator_quantidade = db.Column(db.Float, nullable=False, default=1.0)

    primeira_visto_em = db.Column(db.DateTime, default=datetime.utcnow)
    confirmado_em = db.Column(db.DateTime, nullable=True)
    confirmado_por = db.Column(db.Integer, db.ForeignKey('usuario.id'), nullable=True)

    receita = db.relationship('Receita')
    produto = db.relationship('Produto')
    materia_prima = db.relationship('MateriaPrima')

    @property
    def estado(self):
        if self.ignorar:
            return 'ignorado'
        if self.receita_id or self.produto_id or self.materia_prima_id:
            return 'mapeado'
        return 'pendente'

    @property
    def alvo_nome(self):
        if self.receita:
            return self.receita.nome
        if self.produto:
            return self.produto.nome
        if self.materia_prima:
            return self.materia_prima.nome
        return None

    @property
    def alvo_tipo(self):
        if self.receita_id:
            return 'receita'
        if self.produto_id:
            return 'produto'
        if self.materia_prima_id:
            return 'mp'
        return None


class LojaDebito(db.Model):
    """Acumulador de fracoes pra saida em lote.

    Mesma logica do SeruDebito/VndaDebito: quando fator<1 (ex: 0.2), vender 3
    unidades nao baixa estoque (qtd_efetiva=0.6). A fracao fica aqui ate
    acumular >=1, dai baixa N inteiros. Sem isso, fracoes se perdem.
    """
    __tablename__ = 'loja_debito'

    loja_id = db.Column(db.Integer, db.ForeignKey('loja.id'), primary_key=True)
    loja_produto_map_id = db.Column(db.Integer,
                                     db.ForeignKey('loja_produto_map.id', ondelete='CASCADE'),
                                     primary_key=True)
    fracao_pendente = db.Column(db.Float, nullable=False, default=0.0)
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow,
                               onupdate=datetime.utcnow)

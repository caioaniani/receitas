from datetime import datetime

from app.extensions import db


class MateriaPrima(db.Model):
    __tablename__ = 'materia_prima'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False, unique=True)
    unidade = db.Column(db.String(20), nullable=False)
    preco = db.Column(db.Float, nullable=False)
    fornecedor = db.Column(db.String(100))
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<MateriaPrima {self.nome}>'


class Receita(db.Model):
    __tablename__ = 'receita'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(150), nullable=False)
    categoria = db.Column(db.String(50), nullable=False)
    rendimento_qtd = db.Column(db.Float, nullable=False)
    rendimento_unidade = db.Column(db.String(30), nullable=False)
    margem_lucro = db.Column(db.Float)
    custo_adicional_pct = db.Column(db.Float)
    custo_adicional_fixo = db.Column(db.Float)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    ingredientes = db.relationship(
        'ReceitaIngrediente',
        backref='receita',
        lazy=True,
        cascade='all, delete-orphan'
    )

    @property
    def custo_total(self):
        return sum(
            ing.quantidade * ing.materia_prima.preco
            for ing in self.ingredientes
            if ing.materia_prima is not None
        )

    @property
    def custo_por_unidade(self):
        if self.rendimento_qtd and self.rendimento_qtd > 0:
            return self.custo_total / self.rendimento_qtd
        return 0.0

    @property
    def custo_total_com_adicionais(self):
        base = self.custo_total
        if self.custo_adicional_pct:
            base += base * (self.custo_adicional_pct / 100.0)
        if self.custo_adicional_fixo:
            base += self.custo_adicional_fixo
        return base

    @property
    def preco_venda_sugerido(self):
        if not self.rendimento_qtd or self.rendimento_qtd <= 0:
            return 0.0
        custo_unit = self.custo_total_com_adicionais / self.rendimento_qtd
        if self.margem_lucro:
            return custo_unit * (1 + self.margem_lucro / 100.0)
        return custo_unit

    @property
    def lucro_por_unidade(self):
        if not self.rendimento_qtd or self.rendimento_qtd <= 0:
            return 0.0
        custo_unit = self.custo_total_com_adicionais / self.rendimento_qtd
        return self.preco_venda_sugerido - custo_unit

    def __repr__(self):
        return f'<Receita {self.nome}>'


class ReceitaIngrediente(db.Model):
    __tablename__ = 'receita_ingrediente'

    id = db.Column(db.Integer, primary_key=True)
    receita_id = db.Column(db.Integer, db.ForeignKey('receita.id'), nullable=False)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'), nullable=False)
    quantidade = db.Column(db.Float, nullable=False)
    eh_base = db.Column(db.Boolean, default=False)

    materia_prima = db.relationship('MateriaPrima', lazy=True)

    @property
    def custo(self):
        if self.materia_prima:
            return self.quantidade * self.materia_prima.preco
        return 0.0

    @property
    def porcentagem_padeiro(self):
        if self.eh_base:
            return 100.0
        base_ing = next((i for i in self.receita.ingredientes if i.eh_base), None)
        if base_ing and base_ing.quantidade > 0:
            return (self.quantidade / base_ing.quantidade) * 100.0
        return None

    def __repr__(self):
        return f'<ReceitaIngrediente {self.materia_prima_id} x{self.quantidade}>'

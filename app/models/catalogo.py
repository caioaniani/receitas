"""Modelos do dominio: catalogo.

Faz parte de `app.models` (split em multiplos arquivos por dominio
em 2026-05-21). Importar via `from app.models import X` continua
funcionando porque `app/models/__init__.py` re-exporta tudo.
"""

from app.extensions import db
from app.utils import agora


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
    criado_em = db.Column(db.DateTime, default=agora)

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
    data = db.Column(db.DateTime, default=agora, index=True)
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
    imagem_url = db.Column(db.String(400))  # URL externa pra cardapio digital (fallback)
    imagem_blob = db.Column(db.LargeBinary)  # foto enviada pelo admin (preferida)
    imagem_mimetype = db.Column(db.String(50))
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
    imagem_url = db.Column(db.String(400))  # URL externa pra cardapio digital (fallback)
    imagem_blob = db.Column(db.LargeBinary)  # foto enviada pelo admin (preferida)
    imagem_mimetype = db.Column(db.String(50))
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
